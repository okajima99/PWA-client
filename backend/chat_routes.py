"""チャット送受信・状態問い合わせ系のエンドポイント群。

セッション (UI 上の 1 タブ = 1 議題) を一意キー session_id で扱う。

含まれるルート:
- POST /chat/{session_id}/stream      新規ターン開始 + SSE 配信
- POST /chat/{session_id}/answer      AskUserQuestion への回答
- POST /chat/{session_id}/stop        ターン中断
- GET  /chat/{session_id}/reconnect   バッファ再生
- POST /sessions/{session_id}/end     claude session_id だけクリア (UI セッションは残す)
- GET  /status/{session_id}           ステータス取得
- GET  /sessions                      セッション一覧
- POST /sessions                      新規セッション作成 (body: {agent_id, title?})
- PATCH /sessions/{session_id}        title 変更 (body: {title})
- DELETE /sessions/{session_id}       セッション削除
- GET  /agents                        agent 種別一覧 (作成時の選択肢)
"""
import asyncio
import json
import logging
import uuid
from typing import List

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse

from chat_content import build_content, save_to_tmp
from config import AGENTS
from sdk_runner import disconnect_client, run_sdk_background
from session_logging import (
    delete_session_log,
    mark_session_end,
    prune_session_log,
    session_log,
)
from state import (
    agent_status,
    register_session,
    rename_session,
    reset_activity,
    save_sessions,
    session_tmp_files,
    sessions,
    sessions_meta,
    shared_status,
    stream_states,
    unregister_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --- 共通 helper (chat_stream の new-turn 開始 / chat_stop で重複してた処理) ---
async def _interrupt_and_mark_orphan(state, session_id: str, log_context: str):
    """SDK client が走ってれば interrupt、 実行中の tool_use を orphan として記録。
    次ターン先頭で synthetic tool_result を差し込んで Anthropic API の
    「tool_use ids without tool_result」 400 を回避する用。"""
    if state.client is not None:
        try:
            await state.client.interrupt()
        except Exception:
            logger.exception("interrupt failed %s for session=%s", log_context, session_id)
    cur = agent_status[session_id].get("current_tool")
    if cur and cur.get("id"):
        state.orphaned_tool_use_id = cur["id"]


async def _await_task_cancellation(state):
    """state.task をキャンセルして完全終了を待つ。 Python 3.8+ の CancelledError は
    BaseException 直系なので明示で握り潰す。"""
    if state.task and not state.task.done():
        state.task.cancel()
        try:
            await state.task
        except (Exception, asyncio.CancelledError):
            pass


# --- SSE replay generator (chat_stream / reconnect_stream で共有) ---
async def _sse_replay(state, from_pos: int = 0):
    """state.buffer を from_pos から再生 + 15 秒間隔で keep-alive ping。
    state.complete + sent が buffer 末尾に追いついたら終了。

    state.buffer_event を待つイベント駆動。 mutation 側 (= buffer.append / complete=True /
    buffer reset) が event.set() を呼ぶ前提だが、 wait_for(timeout=15) で必ず wake する
    ので set() を漏らしても最大 15 秒遅延でハングはしない。 timeout 自体が ping 用にも
    使われる二重用途。"""
    sent = max(0, from_pos)
    while True:
        # buffer に未送信ぶんがあれば先に flush
        while sent < len(state.buffer):
            yield state.buffer[sent]
            sent += 1
        if state.complete and sent >= len(state.buffer):
            break
        # event を待つ。 timeout = keep-alive ping 周期。
        # clear() は wait の直前 (= buffer を読み切った後) に行うことで、 wait 中に来た
        # set() を取りこぼさない (= event.set() は idempotent)。
        state.buffer_event.clear()
        # ただし event.clear() と len(state.buffer) の再チェック間に append が来る race
        # を考慮: clear 後にもう一度 buffer / complete を見て、 すでに進展があれば即 loop。
        if sent < len(state.buffer) or state.complete:
            continue
        try:
            await asyncio.wait_for(state.buffer_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            yield ": ping\n\n"


# --- セッション CRUD ---
@router.get("/sessions")
def list_sessions():
    return [m.to_dict() for m in sessions_meta.values()]


@router.post("/sessions")
def create_session(payload: dict = Body(...)):
    agent_id = payload.get("agent_id")
    title = payload.get("title")
    if not agent_id or agent_id not in AGENTS:
        raise HTTPException(status_code=400, detail="agent_id が無効です")
    meta = register_session(agent_id, title)
    return meta.to_dict()


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, payload: dict = Body(...)):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="title は必須 (空不可)")
    rename_session(session_id, title.strip())
    return sessions_meta[session_id].to_dict()


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    # SDK client を切断してから state を破棄
    await disconnect_client(session_id)
    # 残タスクがあればキャンセル
    state = stream_states.get(session_id)
    if state and state.task and not state.task.done():
        state.task.cancel()
        try:
            await state.task
        except (Exception, asyncio.CancelledError):
            # CancelledError は Python 3.8+ で BaseException 直系なので Exception では取れない。
            # 並行 cleanup を継続するため明示的に握り潰す。
            pass
    # 一時ファイルをクリーンアップ
    for p in session_tmp_files.pop(session_id, []):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("tmp file unlink failed: %s", p, exc_info=True)
    # per-tab ログを丸ごと削除
    delete_session_log(session_id)
    unregister_session(session_id)
    return {"status": "ok", "session_id": session_id}


# --- エンドポイント ---
@router.post("/chat/{session_id}/stream")
async def chat_stream(
    session_id: str,
    # message は空でも OK (= 画像 / ファイル単独送信)。 frontend は attachment があれば
    # text 空でも送信ボタンを enable する設計に合わせる。
    message: str = Form(default=""),
    files: List[UploadFile] = File(default=[]),
):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    state = stream_states[session_id]

    # 新ターン開始: 直前のタスクが残っていれば完全にキャンセル・待機する
    # （割り込まれた tool_use は orphan として記録し、下で tool_result を合成して閉じる）
    if not state.complete and state.task and not state.task.done():
        await _interrupt_and_mark_orphan(state, session_id, "during new-stream")
        agent_status[session_id]["current_tool"] = None
        await _await_task_cancellation(state)

    if state.complete or state.task is None or state.task.done():
        saved_files = await save_to_tmp(files, session_id)
        content = build_content(message, saved_files)

        # 孤児 tool_use が残っていれば synthetic tool_result を先頭に差し込んで履歴を閉じる
        # （これをしないと Anthropic API が "tool_use ids without tool_result" で 400 を返し、
        #  以降のターンの推論が空になって表示が 1 ターンずれる）
        if state.orphaned_tool_use_id:
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": state.orphaned_tool_use_id,
                    "content": "User cancelled the previous turn.",
                    "is_error": True,
                },
                *content,
            ]
            state.orphaned_tool_use_id = None

        # user_request_id: この POST 起点ターンを識別する ID。
        # SDK が同じ receive_response で自発ターン (CronCreate/ScheduleWakeup 由来) の
        # ResultMessage を追加で吐くケースがある。ID で送信ボタン解放を ユーザーターンの
        # ResultMessage 1 個に限定し、自発の Result でロックが外れないようにする。
        user_request_id = uuid.uuid4().hex[:12]
        state.user_request_id = user_request_id
        session_log(
            session_id,
            f"[POST /chat/stream] user_request_id={user_request_id} text={message[:80]!r} files={len(saved_files)}",
        )

        state.buffer = []
        state.buffer_id = str(uuid.uuid4())
        # SSE 先頭で request_id をフロントに通知
        import json as _json
        state.buffer.append(
            "data: " + _json.dumps({"type": "request_id", "request_id": user_request_id}) + "\n\n"
        )
        state.complete = False
        # 新 turn 開始: 前 turn の complete=True で set された event を一旦落としてから、
        # 新規 append ぶんを通知 (= replay 側が即 wake して先頭イベントを受け取れる)。
        state.buffer_event.clear()
        state.buffer_event.set()
        state.status_event.set()  # /status SSE に「turn 開始 = streaming true」 を即通知
        state.task = asyncio.create_task(run_sdk_background(session_id, content, user_request_id))

    return StreamingResponse(
        _sse_replay(state, 0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/{session_id}/answer")
async def chat_answer(session_id: str, payload: dict = Body(...)):
    """AskUserQuestion への回答を受け取って can_use_tool ハンドラに返す"""
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    state = stream_states[session_id]
    if state.pending_question is None or state.pending_question.done():
        raise HTTPException(status_code=409, detail="回答待ちの質問がありません")

    answer = payload.get("answer", "")
    if not isinstance(answer, str):
        raise HTTPException(status_code=400, detail="answer は文字列である必要があります")

    state.pending_question.set_result(answer)
    return {"status": "ok", "tool_use_id": state.pending_question_tool_id}


@router.post("/chat/{session_id}/stop")
async def chat_stop(session_id: str):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    state = stream_states[session_id]
    await _interrupt_and_mark_orphan(state, session_id, "from /stop")

    if state.task and not state.task.done():
        # SDK の interrupt で receive_response が終了するはずだが、念のためキャンセルもトリガー
        state.task.cancel()

    if state.pending_question is not None and not state.pending_question.done():
        state.pending_question.cancel()

    # ここでタスクが完全に終わるまで await する。 await しないと、 stop のすぐ後に
    # 次ターンが来た際に古いタスクの finally と新ターンの初期化が race する。
    await _await_task_cancellation(state)

    # interrupt 後の SDK client は内部状態が壊れている可能性があり、再利用すると
    # 次ターンの ResultMessage が is_error=true で帰ってきて「⚠ エラーで停止」
    # チップが出たり、以降のターンで挙動がおかしくなる。明示的に disconnect して
    # 新 send で ensure_client が新しい client を建て直すようにする。
    await disconnect_client(session_id)

    state.complete = True
    state.buffer_event.set()  # replay 側を wake (= /stop 時の SSE クローズを即時化)
    reset_activity(session_id)

    return {"status": "stopped"}


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str):
    """claude 側の会話 context だけリセット (UI セッションは残す)。
    旧 /session/{agent}/end の置換。 セッションそのものを消すには DELETE /sessions/{id}。
    """
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    # SDK クライアントを切断（再接続で新セッションになる）
    await disconnect_client(session_id)
    sessions[session_id] = None
    save_sessions()
    agent_status[session_id]["todos"] = None
    agent_status[session_id]["plan_mode"] = False
    reset_activity(session_id)
    for p in session_tmp_files.pop(session_id, []):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("tmp file unlink failed: %s", p, exc_info=True)
    # per-tab ログにセッション終了マーカーを書いて、 古いセッション分を prune
    mark_session_end(session_id)
    prune_session_log(session_id)
    return {"status": "ok", "session_id": session_id}


def _build_status(session_id: str) -> dict:
    """/status と /status/.../stream で共有する status payload 生成。"""
    a = agent_status[session_id]
    state = stream_states[session_id]
    return {
        "model": a["model"],
        "ctx_pct": a["ctx_pct"],
        "plan_mode": a["plan_mode"],
        "current_tool": a["current_tool"],
        "todos": a["todos"],
        "subagent": a["subagent"],
        "five_hour_pct": shared_status["five_hour_pct"],
        "seven_day_pct": shared_status["seven_day_pct"],
        "five_hour_resets_at": shared_status["five_hour_resets_at"],
        "seven_day_resets_at": shared_status["seven_day_resets_at"],
        "streaming": not state.complete,
        "buffer_length": len(state.buffer),
        "buffer_id": state.buffer_id,
        "pending_question_tool_id": state.pending_question_tool_id,
    }


@router.get("/status/{session_id}")
def get_status(session_id: str):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return _build_status(session_id)


@router.get("/status/{session_id}/stream")
async def status_stream(session_id: str):
    """状態変化を即時 push する SSE。 frontend は EventSource で subscribe して
    polling 撤廃。 state.status_event が set されるたびに最新 status を yield。
    timeout で keep-alive ping、 タブ閉じれば接続が切れて自然終了。"""
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    state = stream_states[session_id]

    async def gen():
        # 接続直後に snapshot を 1 chunk で送る (= retry + initial data を結合し、
        # Starlette の小チャンク buffering を回避)。
        initial = f"retry: 3000\n\ndata: {json.dumps(_build_status(session_id))}\n\n"
        yield initial
        while True:
            try:
                # 20 秒待っても変化無ければ keep-alive ping (= proxy idle 切断対策)
                await asyncio.wait_for(state.status_event.wait(), timeout=20.0)
                state.status_event.clear()
                yield f"data: {json.dumps(_build_status(session_id))}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{session_id}/reconnect")
async def reconnect_stream(session_id: str, from_pos: int = Query(default=0, alias="from")):
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    state = stream_states[session_id]
    if state.complete and from_pos >= len(state.buffer):
        return Response(status_code=204)

    return StreamingResponse(
        _sse_replay(state, from_pos),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agents")
def list_agents():
    """セッション作成時の選択肢として agent 種別一覧を返す。"""
    return [
        {"id": name, "display_name": cfg.get("display_name", name.upper())}
        for name, cfg in AGENTS.items()
    ]


# --- session 別 model / effort 切替 ---
ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


@router.get("/sessions/{session_id}/config")
def get_session_config(session_id: str):
    """session の model / effort 上書き値を返す (= 未設定なら null)。
    UI が現在の選択を表示するため。"""
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    state = stream_states[session_id]
    cfg = AGENTS.get(state.agent_id) or {}
    return {
        "model": state.model_override,
        "effort": state.effort_override,
        "default_model": cfg.get("model"),
        "default_effort": "medium",
    }


@router.patch("/sessions/{session_id}/config")
async def patch_session_config(session_id: str, payload: dict = Body(...)):
    """session の model / effort 上書きを更新する。 None / 未指定で「デフォルトに戻す」。
    変更時は既存 SDK client を disconnect して、 次ターン開始時に新値で建て直す。"""
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    state = stream_states[session_id]
    # 推論中の切替は SDK client を強制切断 → 走ってる receive_response が途中で死ぬ
    # ため、 ストリーム表示が中途半端な状態で固まる。 完了を待つか /stop してからに。
    if not state.complete:
        raise HTTPException(
            status_code=409,
            detail="推論中はモデル / effort 切替不可。 応答完了 or 停止後に再試行してください。",
        )
    changed = False
    if "model" in payload:
        m = payload["model"]
        if m is not None and not isinstance(m, str):
            raise HTTPException(status_code=400, detail="model は文字列か null")
        if state.model_override != m:
            state.model_override = m or None
            changed = True
    if "effort" in payload:
        e = payload["effort"]
        if e is not None:
            if not isinstance(e, str) or e not in ALLOWED_EFFORTS:
                raise HTTPException(status_code=400, detail=f"effort は {ALLOWED_EFFORTS} のいずれか or null")
        if state.effort_override != e:
            state.effort_override = e or None
            changed = True
    if changed:
        # 既存 client を切断 → 次の ensure_client で新 model / effort で建て直し
        await disconnect_client(session_id)
    return {
        "ok": True,
        "model": state.model_override,
        "effort": state.effort_override,
    }
