"""Claude Agent SDK を駆動して SSE wire イベントを buffer に積む層。

設計 (2026-05-17 改修、 持続 receive 型):
    1 セッションあたり 1 個の **persistent_receive_loop** task が SDK の全 message を
    `client._query.receive_messages()` で持続的に受信する。 user POST 経由のターンも
    proactive (Monitor / CronCreate / ScheduleWakeup 等) のターンも、 全部この 1 本の
    async for で拾う。 receive_messages は内部で anyio memory stream を await でブロック
    するので、 メッセージが来ない間は CPU / fd を消費しない (= 前回 fd leak バグの
    「outer while + receive_response 即 return」 を構造的に回避)。

turn ownership 判定:
    UserMessage の content と直近 POST の `state.pending_user_input` を照合し、
    一致なら `state.user_request_id` を current にセット (= user turn 開始)、
    そうでなければ proactive_xxx を current にセット (= 自発 turn 開始)。
    ResultMessage 受信で current = None、 state.complete = True。

main 公開関数:
    - ensure_client(session_id): SDK client 接続 + persistent_receive_loop 起動
    - disconnect_client(session_id): receive task cancel + SDK client disconnect
    - idle_disconnect_loop(): N 秒ごとに idle session を GC
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from config import AGENTS, CLAUDE_PATH
from push import broadcast_push, notification_title_for
from session_logging import close_session_log, session_log
from state import (
    agent_status,
    compute_ctx_pct,
    flags,
    last_assistant_text,
    reset_activity,
    save_sessions,
    sessions,
    shared_status,
    stream_states,
    update_agent_from_result,
)

logger = logging.getLogger(__name__)


# --- SDK メッセージ → CLI stream-json 互換 dict ---
def _block_to_dict(block: Any) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return {"type": "unknown", "raw": str(block)}


def serialize_sdk_message(msg: Any) -> dict | None:
    """SDK Message → フロント互換 JSON dict (CLI stream-json 形式)。"""
    if isinstance(msg, AssistantMessage):
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [_block_to_dict(b) for b in msg.content],
                "usage": msg.usage,
                "model": msg.model,
                "id": msg.message_id,
                "stop_reason": msg.stop_reason,
            },
            "parent_tool_use_id": msg.parent_tool_use_id,
            "session_id": msg.session_id,
            "uuid": msg.uuid,
        }
    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, list):
            content = [_block_to_dict(b) for b in content]
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": msg.parent_tool_use_id,
            "uuid": msg.uuid,
        }
    if isinstance(msg, ResultMessage):
        return {
            "type": "result",
            "subtype": msg.subtype,
            "session_id": msg.session_id,
            "num_turns": msg.num_turns,
            "duration_ms": msg.duration_ms,
            "duration_api_ms": msg.duration_api_ms,
            "is_error": msg.is_error,
            "total_cost_usd": msg.total_cost_usd,
            "usage": msg.usage,
            "modelUsage": msg.model_usage,  # 既存フロント/backend は camelCase
            "result": msg.result,
            "stop_reason": msg.stop_reason,
            "uuid": msg.uuid,
        }
    if isinstance(msg, SystemMessage):
        # TaskStartedMessage / TaskProgressMessage / TaskNotificationMessage は
        # SystemMessage のサブクラス。data dict を展開して top-level に出す。
        wire: dict = {"type": "system", "subtype": msg.subtype}
        if isinstance(msg.data, dict):
            for k, v in msg.data.items():
                if k not in wire:
                    wire[k] = v
        return wire
    if isinstance(msg, RateLimitEvent):
        info = msg.rate_limit_info
        rl_dict = {
            "status": info.status,
            "resetsAt": info.resets_at,
            "rateLimitType": info.rate_limit_type,
            "utilization": info.utilization,
        }
        if info.raw:
            for k, v in info.raw.items():
                rl_dict.setdefault(k, v)
        return {
            "type": "rate_limit_event",
            "rate_limit_info": rl_dict,
            "session_id": msg.session_id,
            "uuid": msg.uuid,
        }
    return None


# --- can_use_tool ハンドラ ---
def make_permission_handler(session_id: str):
    async def handler(tool_name: str, input_data: dict, context: Any):
        if tool_name != "AskUserQuestion":
            return PermissionResultAllow(updated_input=input_data)

        state = stream_states[session_id]
        if state.pending_question is not None and not state.pending_question.done():
            state.pending_question.cancel()

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        state.pending_question = future
        tool_use_id = getattr(context, "tool_use_id", None)
        state.pending_question_tool_id = tool_use_id

        # SSE にも明示的な ask_user_question イベントを積む（フロントが tool_use から
        # 検出するパスと並走。互換のため同じ情報を別タイプでも通知）
        state.buffer.append(
            "data: " + json.dumps({
                "type": "ask_user_question",
                "tool_use_id": tool_use_id,
                "input": input_data,
            }) + "\n\n"
        )
        state.buffer_event.set()
        # status SSE 受信側にも「buffer 増えた」 を通知 (= App.jsx の buffer_length watcher が
        # 質問待ちで止まる直前の最終 append を取りこぼさないため)。
        state.status_event.set()

        # Web Push: アプリが前面表示されてないなら、 質問テキストを通知に流す
        # (回答待ちでロックされるので、 ターン完了まで待つと体感が悪い)
        if not flags["user_visible"]:
            try:
                questions = input_data.get("questions") or []
                first_q = questions[0] if isinstance(questions, list) and questions else {}
                question_text = first_q.get("question") if isinstance(first_q, dict) else None
                if question_text:
                    asyncio.create_task(broadcast_push(
                        f"❓ {question_text}",
                        notification_title_for(session_id),
                        session_id,
                    ))
            except Exception:
                logger.exception("ask_user_question push failed for session=%s", session_id)

        try:
            answer = await future
        except asyncio.CancelledError:
            state.pending_question = None
            state.pending_question_tool_id = None
            return PermissionResultDeny(message="ユーザー応答待ちがキャンセルされました。", interrupt=True)

        state.pending_question = None
        state.pending_question_tool_id = None
        return PermissionResultDeny(message=f"ユーザーの回答: {answer}", interrupt=False)

    return handler


# --- 1 メッセージの処理 (= 旧 run_sdk_background の inner loop と等価) ---
async def _process_message(state, session_id: str, msg: Any) -> None:
    """SDK から受信した 1 メッセージを処理:
    - turn ownership (= current request_id) を判定 / 更新
    - state.complete を turn 境界で切替
    - agent_status の各種フィールド更新
    - wire 形式に変換して state.buffer に積む
    - ターン完了 (ResultMessage) で Web Push 配信

    state.current_request_id は state に持たず、 ローカル変数で持つのが本来だが、
    persistent loop は単一 task で巻いてるので関数間で持ち回す必要なし。 ただし
    複数 message にまたがる current ID 維持のため、 state に generic 属性で保持
    (= setattr で動的に持たせる)。
    """
    # current request_id を state に乗せて関数を跨ぐ
    current_request_id = getattr(state, "_current_request_id", None)

    wire = serialize_sdk_message(msg)

    # --- turn 開始判定 (= 全 message 種共通) ---
    # complete=True の状態で何らかのメッセージ受信 = 新ターン開始シグナル。
    # ResultMessage は turn 終了マーカーなので除外、 subagent 配下のメッセージも除外
    # (= subagent は親 turn の続きとして扱う)。
    #
    # UserMessage を待たずに「最初のメッセージ」 で complete=False に倒す理由:
    # Monitor / CronCreate 等は最初に SystemMessage (= task_notification) で来る、
    # 私の応答が短いと AssistantMessage + ResultMessage が ms 単位で続いて
    # UserMessage が出現しない turn もある。 UserMessage 待ちだと streaming=true 状態が
    # 観測されずに送信ボタンが切り替わらない。
    parent_id = getattr(msg, "parent_tool_use_id", None) if not isinstance(msg, SystemMessage) else None
    is_subagent = parent_id is not None
    is_result = isinstance(msg, ResultMessage)
    # turn 開始判定: 以下のいずれかなら新 turn 扱い
    #   - state.complete=True (= 前 turn 完了、 通常の新 turn)
    #   - current_request_id is None (= persistent_receive_loop 新規起動直後 or
    #     POST ハンドラで state.complete=False に倒した直後)
    # ResultMessage と subagent message は turn 開始判定の対象外。
    if (state.complete or current_request_id is None) and not is_result and not is_subagent:
        # turn 開始: state.user_request_id がセット済み (= POST 直後で未消費) なら user ターン、
        # そうでなければ自発 (Monitor / CronCreate / ScheduleWakeup 等)。
        # SDK の receive_messages は claude API の応答だけを yield、 POST で投入した user
        # input そのものは yield しない (= UserMessage content と照合する旧設計は不可)。
        # 「POST 後の最初のメッセージで user_request_id を current にセット、 ResultMessage で
        # reset」 という生き方で turn ownership を判定する。
        state.complete = False
        state.last_activity_at = time.time()
        state.status_event.set()
        if state.user_request_id is not None and current_request_id is None:
            current_request_id = state.user_request_id
            session_log(
                session_id,
                f"[turn-start] USER user_request_id={current_request_id}",
            )
        else:
            current_request_id = f"proactive_{uuid.uuid4().hex[:8]}"
            session_log(
                session_id,
                f"[turn-start] PROACTIVE request_id={current_request_id}",
            )

    if isinstance(msg, UserMessage):
        # tool_result wrap UserMessage の current_tool 解放処理 (= turn 境界判定は冒頭で済み)
        if not is_subagent and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    cur = agent_status[session_id].get("current_tool")
                    if cur and cur.get("id") == block.tool_use_id:
                        agent_status[session_id]["current_tool"] = None

    elif isinstance(msg, AssistantMessage):
        is_subagent = msg.parent_tool_use_id is not None
        if msg.usage and not is_subagent:
            ctx_window = agent_status[session_id].get("ctx_window") or 1_000_000
            agent_status[session_id]["ctx_pct"] = compute_ctx_pct(msg.usage, ctx_window)
        if not is_subagent:
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    agent_status[session_id]["current_tool"] = {
                        "name": block.name,
                        "id": block.id,
                        "started_at": time.time(),
                    }
                    if block.name == "TodoWrite":
                        todos = block.input.get("todos")
                        if todos is not None:
                            agent_status[session_id]["todos"] = todos
                    elif block.name == "ExitPlanMode":
                        agent_status[session_id]["plan_mode"] = False
            text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
            if text_parts:
                last_assistant_text[session_id] = "\n".join(text_parts)

    elif isinstance(msg, SystemMessage):
        sub = msg.subtype
        if sub == "init":
            perm = msg.data.get("permissionMode") if isinstance(msg.data, dict) else None
            agent_status[session_id]["plan_mode"] = (perm == "plan")
        elif sub == "task_started":
            agent_status[session_id]["subagent"] = {
                "description": msg.data.get("description", "") if isinstance(msg.data, dict) else "",
                "last_tool": "",
                "task_id": msg.data.get("task_id", "") if isinstance(msg.data, dict) else "",
            }
        elif sub == "task_progress":
            cur = agent_status[session_id].get("subagent")
            task_id = msg.data.get("task_id", "") if isinstance(msg.data, dict) else ""
            if cur and cur.get("task_id") == task_id:
                last_tool = msg.data.get("last_tool_name") if isinstance(msg.data, dict) else None
                if last_tool:
                    cur["last_tool"] = last_tool
        elif sub == "task_notification":
            cur = agent_status[session_id].get("subagent")
            task_id = msg.data.get("task_id", "") if isinstance(msg.data, dict) else ""
            if cur and cur.get("task_id") == task_id:
                agent_status[session_id]["subagent"] = None

    elif isinstance(msg, ResultMessage):
        if msg.session_id:
            sessions[session_id] = msg.session_id
            save_sessions()
            state.client_session_id = msg.session_id
        update_agent_from_result(session_id, msg.model_usage, {})

        # ターン完了通知 (= フォアで見てないなら Web Push)
        turn_text = last_assistant_text.get(session_id, "").strip()
        if turn_text and not flags["user_visible"]:
            body = turn_text if len(turn_text) <= 140 else (turn_text[:140] + "…")
            asyncio.create_task(broadcast_push(body, notification_title_for(session_id), session_id))
        last_assistant_text[session_id] = ""

    elif isinstance(msg, RateLimitEvent):
        info = msg.rate_limit_info
        if info.resets_at:
            if info.rate_limit_type and "five_hour" in info.rate_limit_type:
                shared_status["five_hour_resets_at"] = info.resets_at
            elif info.rate_limit_type and "seven_day" in info.rate_limit_type:
                shared_status["seven_day_resets_at"] = info.resets_at

    # --- wire を buffer に積む ---
    if wire is not None:
        # current_request_id がまだ無い場合は proactive で初期化 (= 安全策、 通常は
        # UserMessage で決まる)
        if current_request_id is None:
            current_request_id = f"proactive_{uuid.uuid4().hex[:8]}"
        wire["request_id"] = current_request_id
        state.buffer.append("data: " + json.dumps(wire, ensure_ascii=False) + "\n\n")
        state.buffer_event.set()
        state.status_event.set()
        _suffix = (
            " (user-turn-end)"
            if (isinstance(msg, ResultMessage) and current_request_id == state.user_request_id)
            else ""
        )
        session_log(
            session_id,
            f"[wire] type={wire.get('type')} request_id={current_request_id}{_suffix}",
        )

    # ResultMessage の後は turn 終了 → complete=True、 current ID クリア。
    # ただし「前ターンの遅延 ResultMessage が新ターン開始後に届く」 stale ケースは
    # state.complete を上書きしない (= 新ターンの complete=False を守る)。
    #
    # stale 判定: current_request_id がユーザーターン由来 (user_xxx 形式 = proactive_ で
    # 始まらない) かつ現在の state.user_request_id と異なる = chat_stream で既に新規
    # user_request_id がセット済み → 自分は前ターン由来の遅延 wire。
    if isinstance(msg, ResultMessage):
        is_stale = (
            state.user_request_id is not None
            and current_request_id is not None
            and current_request_id != state.user_request_id
            and not current_request_id.startswith("proactive_")
        )
        if not is_stale:
            state.complete = True
            state.last_activity_at = time.time()
            state.buffer_event.set()
            state.status_event.set()
            reset_activity(session_id)
            if current_request_id == state.user_request_id:
                state.user_request_id = None
        else:
            session_log(
                session_id,
                f"[stale-result] dropped request_id={current_request_id} "
                f"(current user_request_id={state.user_request_id})",
            )
        current_request_id = None  # 次の UserMessage で再決定

    # current を state に書き戻す (= 次のメッセージで使う)
    state._current_request_id = current_request_id


# --- 持続 receive task (= 1 セッション 1 個) ---
async def persistent_receive_loop(session_id: str) -> None:
    """SDK の `receive_messages` で全 message を持続受信する。

    receive_messages は内部で anyio MemoryObjectStream の `async for` を回しているので、
    メッセージが来ない間は `await` でブロック (= tight loop は構造的に起きない)。
    `client.disconnect()` → `query.close()` で stream が閉じられると async for が
    自然に終了する。
    """
    state = stream_states.get(session_id)
    if state is None or state.client is None:
        return
    client = state.client
    session_log(session_id, "[persistent-receive] started")
    try:
        async for raw in client._query.receive_messages():
            if not isinstance(raw, dict):
                continue
            msg = parse_message(raw)
            if msg is None:
                continue
            try:
                await _process_message(state, session_id, msg)
            except Exception:
                logger.exception("_process_message failed for session=%s", session_id)
    except asyncio.CancelledError:
        session_log(session_id, "[persistent-receive] cancelled")
        raise
    except Exception:
        logger.exception("persistent_receive_loop crashed for session=%s", session_id)
    finally:
        # 終了時は必ず complete=True に倒して UI 解放 + status push
        state.complete = True
        state.buffer_event.set()
        state.status_event.set()
        session_log(session_id, "[persistent-receive] ended")


# --- SDK クライアントの生成/接続 ---
async def ensure_client(session_id: str) -> ClaudeSDKClient:
    state = stream_states[session_id]
    if state.client is not None:
        return state.client

    agent_id = state.agent_id
    cfg = AGENTS[agent_id]
    # session override > config の model、 override > "medium" の effort。
    effort = state.effort_override or "medium"
    model = state.model_override or cfg.get("model") or None
    env = {
        "ANTHROPIC_BASE_URL": "http://localhost:8000/proxy",
        "CLAUDE_CODE_EFFORT_LEVEL": effort,
    }
    options = ClaudeAgentOptions(
        cwd=cfg["cwd"],
        resume=sessions.get(session_id),
        setting_sources=["user", "project", "local"],
        can_use_tool=make_permission_handler(session_id),
        allowed_tools=[],  # 空 = 全許可（can_use_tool は AskUserQuestion だけ介入）
        permission_mode="bypassPermissions",
        env=env,
        cli_path=CLAUDE_PATH,
        **({"model": model} if model else {}),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    state.client = client
    state.client_session_id = sessions.get(session_id)
    state._current_request_id = None  # type: ignore[attr-defined]
    # 持続 receive task 起動 (既に走ってないことを確認)
    if state.receive_task is None or state.receive_task.done():
        state.receive_task = asyncio.create_task(persistent_receive_loop(session_id))
    return client


async def disconnect_client(session_id: str) -> None:
    """SDK client を切断する。 持続 receive task も cancel + await。
    turn 系の transient state (= user_request_id / orphan / current ID) も明示リセットして、
    次の ensure_client → POST で前 turn の残骸が混入しないようにする。"""
    state = stream_states.get(session_id)
    if state is None:
        return
    client = state.client
    # 持続 receive task 停止
    if state.receive_task is not None and not state.receive_task.done():
        state.receive_task.cancel()
        try:
            await state.receive_task
        except (Exception, asyncio.CancelledError):
            pass
    state.receive_task = None
    # 切断時の transient state クリア (= 前 turn の user_request_id / current ID が残ってると
    # 次 POST で 自発ターンが誤って USER ターン扱いされる race を防ぐ)。
    # orphaned_tool_use_id は chat_stop 経路で「次 POST に synthetic tool_result を注入する」
    # 用途で意図的に残す設計、 ここではクリアしない。 完全リセットしたい呼び出し側
    # (= end_session / DELETE) は disconnect_client 後に明示クリアする。
    state.user_request_id = None
    state._current_request_id = None  # type: ignore[attr-defined]
    if client is None:
        return
    # 先に参照を切る (この時点で並行 ensure_client は新 client を立て直す)
    state.client = None
    state.client_session_id = None
    try:
        await client.disconnect()
    except Exception:
        logger.exception("disconnect failed for session=%s", session_id)


# --- アイドル GC ---
# 直近のターン完了から IDLE_DISCONNECT_SEC 経過した SDK client を disconnect する。
# claude API の prompt cache が 5 分 TTL なので、 同期間で切るのが妥当 (cache 切れた
# client を保持してもメモリだけ食って効果は無い)。 buffer も同時にクリアして
# F の問題 (再接続不要なバッファ残留) も解消する。
IDLE_DISCONNECT_SEC = 5 * 60
IDLE_GC_INTERVAL_SEC = 60


async def idle_disconnect_loop():
    """N 秒間隔で全セッションを巡回し、 アイドル時間が閾値超のものを disconnect する。"""
    while True:
        try:
            await asyncio.sleep(IDLE_GC_INTERVAL_SEC)
            now = time.time()
            for session_id, state in list(stream_states.items()):
                if state.client is None:
                    continue
                # 進行中ターン or pending question があればスキップ
                if not state.complete:
                    continue
                if state.pending_question is not None and not state.pending_question.done():
                    continue
                # last_activity_at == 0.0 はまだ発話してないセッション (= 立ち上げ済みだが
                # ターン未経験) → GC 対象にしない方が安全
                if state.last_activity_at <= 0:
                    continue
                idle = now - state.last_activity_at
                if idle < IDLE_DISCONNECT_SEC:
                    continue
                session_log(
                    session_id,
                    f"[idle-gc] disconnecting idle={idle:.0f}s",
                )
                try:
                    await disconnect_client(session_id)
                except Exception:
                    logger.exception("idle-gc disconnect failed for session=%s", session_id)
                # buffer は残す: PWA を長時間離れたあとに「最新を取得」 で直近ターンを
                # 復元できるようにするため。 次ターン開始時に chat_routes で空にされる
                # ので memory はターン 1 個ぶんで bound される。
                # ログハンドルも閉じて fd を解放する。 次の発話で勝手に開き直される
                close_session_log(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("idle_disconnect_loop iteration failed")
