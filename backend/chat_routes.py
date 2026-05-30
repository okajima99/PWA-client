"""チャット送受信・状態問い合わせ系のエンドポイント群。

セッション (UI 上の 1 タブ = 1 議題) を一意キー session_id で扱う。

含まれるルート:
- GET  /status/{session_id}           ステータス取得 (+ /stream で SSE push)
- GET  /sessions                      セッション一覧
- POST /sessions                      新規セッション作成 (body: {agent_id, title?})
- PATCH /sessions/{session_id}        title 変更 (body: {title})
- DELETE /sessions/{session_id}       セッション削除
- GET  /agents                        agent 種別一覧 (作成時の選択肢)
- GET/PATCH /sessions/{session_id}/config  model / effort 上書き

チャット送受信そのものは PTY 経路 (pty_routes /pty/{sid}/send) + JSONL SSE
(jsonl_routes /jsonl/stream/{sid}) が担う。 ここは session メタ / status / config 専任。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from config import AGENTS
from usage import read_latest_rate_limits
from state import (
    agent_status,
    backend_start_time,
    register_session,
    rename_session,
    session_tmp_files,
    sessions_meta,
    sessions_overview_event,
    shared_status,
    stream_states,
    unregister_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def require_session(session_id: str) -> str:
    """path の session_id が存在しなければ 404 を投げる FastAPI 依存。 各 endpoint で
    重複していた存在チェックを 1 箇所に集約する (= Depends(require_session) で受ける)。"""
    if session_id not in sessions_meta:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session_id


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
def patch_session(session_id: str, payload: dict = Body(...), _: str = Depends(require_session)):
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="title は必須 (空不可)")
    rename_session(session_id, title.strip())
    return sessions_meta[session_id].to_dict()


@router.post("/sessions/{session_id}/restart")
async def restart_session(session_id: str, _: str = Depends(require_session)):
    """claude プロセスを kill + 新規 spawn する (= /clear と違ってプロセスメモリも完全解放)。
    新 claude_sid に切り替わるが SessionStart hook で bindings 更新されるので、 PWA タブは
    シームレスに続けて使える。 長期稼働で claude プロセスメモリが累積する問題への対策。"""
    from pty_runner import kill_tmux_session, pty_sessions  # noqa: PLC0415
    import jsonl_watcher  # noqa: PLC0415
    from pty_routes import ensure_pty_session_for  # noqa: PLC0415
    # kill 経路は delete_session と同じだが、 sessions_meta は維持して即 spawn し直す
    try:
        kill_tmux_session(session_id)
        pty_sessions.pop(session_id, None)
        jsonl_watcher.unregister(session_id)
    except Exception:
        logger.debug("restart kill phase failed for %s", session_id, exc_info=True)
    # 新規 spawn (= 同 PWA_SID で tmux 再生成 + claude 再起動 + SessionStart hook で
    # 新 claude_sid を confirm_bind)
    try:
        await ensure_pty_session_for(session_id)
    except Exception:
        logger.exception("restart spawn phase failed for %s", session_id)
        return {"ok": False, "reason": "spawn_failed"}
    # agent_status の進行中フラグをリセット (= 新プロセスなので何も保留してない)
    a = agent_status.get(session_id)
    if a is not None:
        a["current_tool"] = None
        a["pending_question"] = None
        a["pending_plan"] = None
        a["subagent"] = None
        a["plan_mode"] = False
    state = stream_states.get(session_id)
    if state is not None:
        state.status_event.set()
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, _: str = Depends(require_session)):
    # PTY + tmux + JSONL binding を一括 cleanup
    try:
        from pty_runner import kill_tmux_session, pty_sessions  # noqa: PLC0415
        import jsonl_watcher  # noqa: PLC0415
        kill_tmux_session(session_id)
        pty_sessions.pop(session_id, None)
        jsonl_watcher.unregister(session_id)
    except Exception:
        logger.debug("session cleanup failed for %s", session_id, exc_info=True)
    # 一時ファイルをクリーンアップ
    for p in session_tmp_files.pop(session_id, []):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("tmp file unlink failed: %s", p, exc_info=True)
    unregister_session(session_id)
    return {"status": "ok", "session_id": session_id}


def _build_status(session_id: str) -> dict:
    """/status と /status/.../stream で共有する status payload 生成。

    使用率系 (5h/7d/ctx/model) は proxy を使わず rate-limits.jsonl (= statusline 記録)
    から取る。 取れない項目は従来の shared_status / agent_status に fallback。

    model / ctx は session ごとに違うので、 この pwa session に紐づく claude_sid
    (= 確定 binding の jsonl ファイル名) で rate-limits を絞る。 これでタブ切替時に
    そのタブの最新ステータスラインが出る (= 別タブの値に引っ張られない)。
    """
    a = agent_status[session_id]
    state = stream_states[session_id]
    import jsonl_watcher  # noqa: PLC0415
    jp = jsonl_watcher.get_jsonl_for(session_id)
    claude_sid = jp.stem if jp else None
    rl = read_latest_rate_limits(claude_sid)
    return {
        "model": rl.get("model") or a["model"],
        "ctx_pct": rl["context_pct"] if rl.get("context_pct") is not None else a["ctx_pct"],
        "plan_mode": a["plan_mode"],
        "current_tool": a["current_tool"],
        "todos": a["todos"],
        "subagent": a["subagent"],
        "pending_plan": a.get("pending_plan"),
        "pending_question": a.get("pending_question"),
        "pending_prompt": a.get("pending_prompt"),
        "five_hour_pct": rl["five_hour_pct"] if rl.get("five_hour_pct") is not None else shared_status["five_hour_pct"],
        "seven_day_pct": rl["seven_day_pct"] if rl.get("seven_day_pct") is not None else shared_status["seven_day_pct"],
        "five_hour_resets_at": rl.get("five_hour_resets_at") or shared_status["five_hour_resets_at"],
        "seven_day_resets_at": rl.get("seven_day_resets_at") or shared_status["seven_day_resets_at"],
        # streaming / buffer_* は撤去 (= PTY 経路では state.complete が常に True 固定の
        # ゴーストフィールドだった)。 frontend の停止/送信ボタン判定は loading (= JSONL
        # SSE の assistant/result で駆動) + pendingSend + pending_question に一本化した。
        "pending_question_tool_id": state.pending_question_tool_id,
        # backend プロセスの起動時刻 (= frontend がこの値の変化で「再起動された」 と検知し、
        # 古い streaming bubble を強制的に停止扱いに固定する)。
        "backend_start_time": backend_start_time,
    }


@router.get("/status/{session_id}")
def get_status(session_id: str, _: str = Depends(require_session)):
    return _build_status(session_id)


@router.get("/status/{session_id}/stream")
async def status_stream(session_id: str, _: str = Depends(require_session)):
    """状態変化を即時 push する SSE。 frontend は EventSource で subscribe して
    polling 撤廃。 state.status_event が set されるたびに最新 status を yield。
    timeout で keep-alive ping、 タブ閉じれば接続が切れて自然終了。"""
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
                # keep-alive 兼 status 更新: TUI 経路は status_event がほぼ発火しないので、
                # この timeout で rate-limits 込みの最新 status を定期 push する
                # (= 5h/7d を ~20 秒粒度で更新)。
                yield f"data: {json.dumps(_build_status(session_id))}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_sessions_overview() -> dict:
    """全 session の busy / pending_question を 1 dict で返す (= /sessions/overview/stream payload)。

    busy は monitor_all_sessions_loop が JSONL から算出した backend 権威値 (= chat SSE の
    result 配信に依存しない)。 frontend は各 sid の busy で loading を上書きして、 青丸
    (処理中) / 赤丸 (完了未読) / 停止ボタンを **非アクティブタブでも** live 追従させる。"""
    out: dict[str, dict] = {}
    for sid in list(sessions_meta.keys()):
        st = stream_states.get(sid)
        a = agent_status.get(sid) or {}
        out[sid] = {
            "busy": bool(st.busy) if st is not None else False,
            "pending_question": bool(a.get("pending_question")),
        }
    return out


@router.get("/sessions/overview/stream")
async def sessions_overview_stream():
    """全 session の busy / pending を 1 本で push する SSE (= 案 B)。

    タブごとに SSE を張らず 1 接続で全 session をカバーするので、 session 数が増えても
    接続は 1 本のまま (= リソース増加なし)。 sessions_overview_event が set されるたびに
    最新 snapshot を yield。 20 秒の timeout で keep-alive 兼 定期同期。

    注: event は全接続で共有するため、 複数デバイスで同時に開くと clear 競合で片方の即時
    push を取りこぼしうるが、 その場合も 20 秒の定期 push で追従する (= 単一デバイス運用が
    主なので実用上の遅延は出ない)。"""
    async def gen():
        # 接続直後に snapshot を 1 chunk で送る (= retry + 初期 data を結合)。
        yield f"retry: 3000\n\ndata: {json.dumps(_build_sessions_overview())}\n\n"
        while True:
            try:
                await asyncio.wait_for(sessions_overview_event.wait(), timeout=20.0)
                sessions_overview_event.clear()
                yield f"data: {json.dumps(_build_sessions_overview())}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps(_build_sessions_overview())}\n\n"

    return StreamingResponse(
        gen(),
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
# 4.8 で追加された値: "auto" (= 内容に応じて effort 自動調整) / "ultracode" (= xhigh +
# auto workflow セット)。 実機で `/effort <値>` が通るかは別途確認 (= 通らなければ claude が
# 未知コマンドとして無視するだけ)。
ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh", "max", "auto", "ultracode"}


@router.get("/sessions/{session_id}/config")
def get_session_config(session_id: str, _: str = Depends(require_session)):
    """session の model / effort 上書き値を返す (= 未設定なら null)。
    UI が現在の選択を表示するため。"""
    state = stream_states[session_id]
    cfg = AGENTS.get(state.agent_id) or {}
    return {
        "model": state.model_override,
        "effort": state.effort_override,
        "fast": state.fast_mode,
        "default_model": cfg.get("model"),
        "default_effort": "medium",
    }


@router.patch("/sessions/{session_id}/config")
async def patch_session_config(session_id: str, payload: dict = Body(...), _: str = Depends(require_session)):
    """session の model / effort 上書きを更新する。 None / 未指定で「デフォルトに戻す」。
    PTY 経路では state 値だけでなく claude TUI に slash command を流して
    実切替まで完遂する (= `/model <name>` / `/effort <level>`)。"""
    state = stream_states[session_id]
    # 推論中の切替ガードは設けない。 claude TUI 側で「推論中の /model」 が効かなければ
    # tmux send-keys が黙って吸われるだけ (= UI 上は変えたつもりで実切替されない)、
    # ユーザが完了後に再試行する想定。
    changed_model = False
    changed_effort = False
    changed_fast = False
    if "model" in payload:
        m = payload["model"]
        if m is not None and not isinstance(m, str):
            raise HTTPException(status_code=400, detail="model は文字列か null")
        if state.model_override != m:
            state.model_override = m or None
            changed_model = True
    if "effort" in payload:
        e = payload["effort"]
        if e is not None:
            if not isinstance(e, str) or e not in ALLOWED_EFFORTS:
                raise HTTPException(status_code=400, detail=f"effort は {ALLOWED_EFFORTS} のいずれか or null")
        if state.effort_override != e:
            state.effort_override = e or None
            changed_effort = True
    if "fast" in payload:
        # `/fast` はトグル (= 引数なしで ON⇄OFF 反転)。 PWA が持つ希望状態と現状が食い違う
        # 時だけ 1 回打って同期する。 2 連打すると ON→OFF に戻ってしまうので差分判定必須。
        f = bool(payload["fast"])
        if state.fast_mode != f:
            state.fast_mode = f
            changed_fast = True
    # PTY 経路: claude TUI に slash command を tmux send-keys で投入する (= 実切替)。
    # 失敗 (= tmux session が無い、 claude TUI が引数取らない、 等) でも 200 で返す:
    # state の override 値は更新されてるので UI 表示は新値、 ユーザが必要なら再試行する。
    from pty_runner import tmux_send_keys
    if changed_model and state.model_override:
        tmux_send_keys(session_id, text=f"/model {state.model_override}", enter=True)
    if changed_effort and state.effort_override:
        # claude TUI に `/effort <level>` コマンドが存在するかは要実機確認 (= 公式 docs に
        # 明示記載なし)。 存在しない場合は claude が「未知コマンド」 として無視する、
        # その時は実装側で対応案を再検討する。
        tmux_send_keys(session_id, text=f"/effort {state.effort_override}", enter=True)
    if changed_fast:
        # `/fast` は引数なしのトグル。 差分時のみ 1 回打鍵 (= 上の差分判定で担保)。
        tmux_send_keys(session_id, text="/fast", enter=True)
    return {
        "ok": True,
        "model": state.model_override,
        "effort": state.effort_override,
        "fast": state.fast_mode,
    }
