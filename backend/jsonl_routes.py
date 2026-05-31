"""claude の JSONL ログを tail して SSE で配信する route (= chat UI の出力側)。

claude を PTY/TUI 経路で動かすと、 会話の全 turn が構造化された JSONL
(`~/.claude/projects/<cwd-hash>/<claude_session_id>.jsonl`) に追記される。 これを
backend が tail し、 jsonl_events で processStreamEvent.js の event 形式に変換して
SSE で流すことで、 proxy/SDK/`-p` を一切使わず (= subscription 枠・軽い) chat UI を
再構成できる。

入出力分離: 出力 (= 表示) はこの SSE、 入力 (= キー送信) は pty_routes の WebSocket。

wire (= SSE):
    data: {<processStreamEvent event>}\n\n   会話 event (assistant / user / result 等)
    : keep-alive\n\n                          ハートビート (= idle 時)
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jsonl_events import jsonl_line_to_events
from jsonl_notifications import maybe_push_blockers as _maybe_push_blockers
from jsonl_session_status import (
    attach_duration_to_result as _attach_duration_to_result,
    compute_busy_from_tail as _compute_busy_from_tail,
    is_user_prompt as _is_user_prompt,
    latest_subagent_tool as _latest_subagent_tool,
    mutate_agent_status as _mutate_agent_status,
    refresh_subagent_status as _refresh_subagent_status,
    track_turn_start as _track_turn_start,
    update_busy as _update_busy,
)
from jsonl_tail import (
    read_complete_lines as _read_complete_lines,
    read_tail as _read_tail,
)
from pty_runner import jsonl_path_for_session
from state import sessions_overview_event, stream_states


logger = logging.getLogger(__name__)

router = APIRouter()

# 初回接続時に遡って replay する最大行数。 frontend は localStorage に最終 byte offset を
# 保存して `?from=<offset>` で渡してくるので、 これは初訪問 / localStorage が消えた時の
# フォールバックとして使われる。
INITIAL_REPLAY_LINES = 500

# tail の polling 間隔 (秒)。
POLL_INTERVAL = 0.5


def _latest_jsonl(session_id: str) -> Path | None:
    """PWA session_id から claude JSONL を解決する。

    実装は pty_runner.jsonl_path_for_session (= tmux pane → claude PID → lsof で
    open file を直接取得) に委譲する。 同じ cwd で動く他の claude プロセス
    (Claude Desktop App / ターミナル直叩き) の JSONL を絶対に拾わない。

    解決失敗時 (= tmux 未生成 / claude 未起動 / lsof で JSONL 未検出) は None。
    """
    return jsonl_path_for_session(session_id)


def _lines_to_sse(lines: list[str], pos: int, session_id: str) -> list[str]:
    """JSONL 行 (文字列) のリストを SSE フレームのリストに変換する。

    各フレームに `id: <pos>` (= この行群を読み終えた後のバイト位置) を付ける。 EventSource は
    受信した最後の id を保持し、 再接続時に `Last-Event-ID` ヘッダで送るので、 backend は
    そこから続きだけ流せる (= backend 再起動後の全 replay を回避)。

    副作用: 各行で `_mutate_agent_status` を呼び、 todos / plan_mode / current_tool /
    ctx_pct / model を更新する。 変化があれば最後に status_event.set() を打って
    `/status/{sid}/stream` SSE を即時 push (= ActivityBar / StopReasonChip を再描画)。
    """
    frames: list[str] = []
    state = stream_states.get(session_id)
    status_dirty = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _track_turn_start(session_id, obj)
        if _mutate_agent_status(session_id, obj):
            status_dirty = True
        # 通知 push 発火 (= _maybe_push_blockers) は SSE 経路で呼ばない。 別 lifespan task の
        # monitor_all_sessions_loop が全 sid を常時 tail して push を担当 (= PWA 接続有無に
        # 関係なく通知発火させるため + SSE 経路との二重発火回避)。
        evts = jsonl_line_to_events(obj)
        _attach_duration_to_result(session_id, obj, evts)
        for event in evts:
            frames.append(f"id: {pos}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
    if status_dirty and state is not None:
        state.status_event.set()
    return frames


def _initial_offset(path: Path) -> int:
    """初回 replay の開始バイト位置。 直近 INITIAL_REPLAY_LINES 行ぶんに絞る。

    末尾から固定 chunk ずつ遡って改行を数え、 「末尾から N 個目の改行の直後」 を返す。
    ファイル全体をメモリに読まないので大きい JSONL でも O(末尾) で済む。 改行が
    INITIAL_REPLAY_LINES 個以下なら 0 (= 全件 replay)。 旧実装 (= 全読み + rfind) と
    同じ境界 (= count <= N → 0、 count > N → N 個目直後) を保つ。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    chunk_size = 64 * 1024
    found = 0
    candidate = 0  # 末尾から N 個目の改行直後。 N+1 個目が見つかったら (= count > N) 返す
    pos = size
    try:
        with open(path, "rb") as f:
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                for i in range(len(chunk) - 1, -1, -1):
                    if chunk[i] != 0x0A:  # b"\n"
                        continue
                    found += 1
                    if found == INITIAL_REPLAY_LINES:
                        candidate = pos + i + 1
                    elif found > INITIAL_REPLAY_LINES:
                        return candidate
    except OSError:
        return 0
    return 0


async def _jsonl_sse(session_id: str, start_pos: int | None = None):
    # チャット画面のみ開いてターミナル画面に切り替えていないタブでも claude を起動させる。
    # 既に tmux + claude が動いていれば no-op。
    from pty_routes import ensure_pty_session_for
    await ensure_pty_session_for(session_id)

    path = _latest_jsonl(session_id)
    if path is None:
        yield f"data: {json.dumps({'type': 'error', 'message': 'no JSONL found for session'})}\n\n"
        return

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    # 再接続 (= Last-Event-ID あり) は続きから、 初回は直近 N 行に絞る。
    # start_pos がファイルサイズを超える (= 別ファイルに切り替わった等) 場合は初回扱い。
    if start_pos is not None and 0 <= start_pos <= size:
        pos = start_pos
    else:
        pos = _initial_offset(path)

    # 初回 replay (= 再接続時は start_pos 以降のみ = 差分)
    lines, pos = _read_complete_lines(path, pos)
    for frame in _lines_to_sse(lines, pos, session_id):
        yield frame

    # tail: 新規追記行を追従する (= stat/truncate/read は _read_tail に集約)
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        lines, pos, status = _read_tail(path, pos)
        if status == "error":
            # ファイルが消えた (= セッション破棄等) → 終了
            return
        if status == "truncated":
            # truncate / rotate → 先頭から読み直す
            lines, pos, status = _read_tail(path, 0)
        emitted = False
        if status == "ok":
            for frame in _lines_to_sse(lines, pos, session_id):
                yield frame
                emitted = True
        # Task 実行中は main JSONL が静かでも subagent は別ファイルで動くので毎 tick 追う。
        # 変化があれば status_event を叩いて /status SSE 経由で last_tool を push する。
        if _refresh_subagent_status(session_id, path):
            st = stream_states.get(session_id)
            if st is not None:
                st.status_event.set()
        if not emitted:
            yield ": keep-alive\n\n"


@router.get("/jsonl/_debug/bindings")
async def jsonl_debug_bindings() -> dict:
    """debug: 現在 backend mem に持ってる watcher binding 一覧。"""
    import jsonl_watcher
    return jsonl_watcher.list_bindings()


@router.get("/jsonl/stream/{session_id}")
async def jsonl_stream(session_id: str, request: Request):
    """指定 PWA session の claude JSONL を tail して SSE で event を流す。

    再接続時は EventSource が送る `Last-Event-ID` (= 前回読み終えた byte 位置) から
    続きだけ流し、 backend 再起動後の全 replay を避ける。
    """
    # 再開位置: EventSource 自動再接続の Last-Event-ID を優先、 無ければ ?from クエリ
    # (= タブ切替で frontend が保持した offset から差分取得する経路)。
    src = request.headers.get("last-event-id") or request.query_params.get("from")
    start_pos: int | None = None
    if src:
        try:
            start_pos = int(src)
        except (ValueError, TypeError):
            start_pos = None
    return StreamingResponse(
        _jsonl_sse(session_id, start_pos),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- 常時 tail (= PWA 接続有無に関係なく動く push 発火経路) ---
# backend の lifespan task として全 PWA session の JSONL を polling し、
# AskUserQuestion 発火 / stop_reason 異常を検出して Web Push を飛ばす。
# SSE 経路 (= /jsonl/stream) の _maybe_push_blockers 呼び出しは廃止済 (= 二重発火回避)。
async def monitor_all_sessions_loop():
    """全 PWA session の JSONL を常時 tail し、 推論を止める要因を検出して push 発火する。

    起動時は各 sid を末尾 offset から開始する (= backend 起動前の過去行は通知しない)。
    `/clear` 等で claude_sid が切り替わると `_latest_jsonl` が新 path を返すので、
    そのときは新 path の末尾から再開する。 file が縮んだ (rotate / truncate) 場合も
    同様に末尾再同期。

    State: state[sid] = (path, byte_offset)。 SSE 経路の `offsetRef` とは独立した
    バックエンド内の追跡 (= frontend の localStorage が消えても影響を受けない)。
    """
    state: dict[str, tuple[Path, int]] = {}
    logger.info("monitor_all_sessions_loop started")
    try:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                from state import sessions_meta as _sessions_meta  # 動的参照
                # 削除済み session の追跡 entry を刈り取る (= 無停止運用での単調増加防止)
                for stale in [s for s in state if s not in _sessions_meta]:
                    state.pop(stale, None)
                for sid in list(_sessions_meta.keys()):
                    path = _latest_jsonl(sid)
                    if path is None:
                        continue
                    prev = state.get(sid)
                    if prev is None or prev[0] != path:
                        # 初回 or path 切替: 末尾から開始 (= 過去行を再通知しない)
                        try:
                            state[sid] = (path, path.stat().st_size)
                        except OSError:
                            pass
                        # busy は過去行を通知しない代わりに末尾から現在値を 1 回算出する
                        # (= backend 起動時に推論中だった session も正しく busy=True にする)。
                        st = stream_states.get(sid)
                        if st is not None:
                            new_busy = _compute_busy_from_tail(path)
                            if new_busy != st.busy:
                                st.busy = new_busy
                                sessions_overview_event.set()
                        continue
                    lines, new_pos, status = _read_tail(path, prev[1])
                    if status == "error":
                        continue
                    # truncated → 末尾再同期 (new_pos=size) / ok → 進行 / nochange → 据置
                    state[sid] = (path, new_pos)
                    if status != "ok":
                        continue
                    for raw in lines:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        _maybe_push_blockers(sid, obj)
                        _update_busy(sid, obj)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor_all_sessions_loop iteration failed")
    except asyncio.CancelledError:
        logger.info("monitor_all_sessions_loop cancelled")
        raise
