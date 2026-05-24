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
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config import TMUX_SESSION_MAP_DIR
from jsonl_events import jsonl_line_to_events
from pty_routes import _resolve_cwd
from pty_runner import _tmux_session_name
from state import agent_status, stream_states
from usage import compute_ctx_pct, format_model_name

logger = logging.getLogger(__name__)

router = APIRouter()

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# statusline が「tmux session 名 → claude session id」 を 1 session = 1 ファイルで書き出す
# ディレクトリ (= config 経由)。 複数タブが同じ cwd を共有しても、 これで JSONL を一意に
# 特定できる (= 単純な最新 mtime だと別タブの会話が混入する)。 未設定なら None。
TMUX_SESSION_MAP = Path(TMUX_SESSION_MAP_DIR).expanduser() if TMUX_SESSION_MAP_DIR else None

# 初回接続時に遡って replay する最大行数 (= 長い履歴で初回ペイロードが膨らむのを防ぐ)。
# frontend は localStorage に最終 byte offset を保存して `?from=<offset>` で渡してくるので、
# 「初めて開くタブ」 や localStorage を消した時のフォールバックとして使われる。 ここを
# 小さくすればタブ切替がさらに軽くなる、 ただし長い履歴を初訪問で取りこぼす量も増える。
INITIAL_REPLAY_LINES = 500

# tail の polling 間隔。 JSONL は message 確定単位 (= 1〜数秒粒度) で追記されるので
# 0.5s で十分追従でき、 かつ CPU を食わない。
POLL_INTERVAL = 0.5


def _cwd_to_project_dir(cwd: str) -> Path:
    """cwd を claude projects のフォルダ名に変換する。

    claude Code の規則: パス中の `/` と `.` を `-` に置換 (先頭 `/` も `-` になる)。
    例: /Users/me/projects/foo → -Users-me-projects-foo
    """
    safe = cwd.replace("/", "-").replace(".", "-")
    return CLAUDE_PROJECTS / safe


def _claude_sid_for(session_id: str) -> str | None:
    """statusline が記録した tmux session 名 → claude session id を引く。"""
    if TMUX_SESSION_MAP is None:
        return None
    f = TMUX_SESSION_MAP / _tmux_session_name(session_id)
    if f.is_file():
        sid = f.read_text(encoding="utf-8", errors="replace").strip()
        return sid or None
    return None


def _latest_jsonl(session_id: str) -> Path | None:
    """PWA session_id から、 対応する claude セッションの JSONL ファイルを解決する。

    厳密解決: statusline が記録した tmux↔claude_sid マップで JSONL を一意特定する
    (= 同じ cwd を共有する複数タブを区別)。 マップが無ければ cwd フォルダの最新 mtime に
    fallback (= 単一セッション時は十分、 hook 記録前の既存セッション救済)。
    """
    cwd = _resolve_cwd(session_id)
    if not cwd:
        return None
    proj = _cwd_to_project_dir(str(Path(cwd).expanduser()))
    if not proj.is_dir():
        return None
    # 厳密: session-map から claude_sid → そのファイルを直接指す
    claude_sid = _claude_sid_for(session_id)
    if claude_sid:
        exact = proj / f"{claude_sid}.jsonl"
        if exact.is_file():
            return exact
    # fallback: 最新 mtime
    jsonls = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonls[0] if jsonls else None


def _read_complete_lines(path: Path, pos: int) -> tuple[list[str], int]:
    """pos (= バイト位置) から読み、 改行で終わる完全な行だけ返す。

    書き込み途中の不完全行 (= 末尾が \\n でない) は次回に持ち越すため、 pos は最後の
    完全行の直後までしか進めない。 返り値 (完全行のリスト, 新 pos)。
    """
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:
        return [], pos
    if not data:
        return [], pos
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        # 完全行がまだ無い (= 書き込み途中)
        return [], pos
    complete = data[: last_nl + 1]
    new_pos = pos + len(complete)
    text = complete.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln]
    return lines, new_pos


# sid → 直近 user 発話 (= turn 開始) の unix epoch。 stop_reason 確定行を見たら
# (現在の確定行の timestamp - 開始) を duration_ms として result event に inject する。
# プロセス内 dict なので backend 再起動で消える、 中断中の turn は duration 取得不可。
_turn_started_at: dict[str, float] = {}


def _parse_jsonl_timestamp(ts: str | None) -> float | None:
    """JSONL 行の `timestamp` (= ISO 8601 "Z" 終端) を unix epoch に変換。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _track_turn_start(session_id: str, line: dict) -> None:
    """素プロンプト (= ユーザ発言) の user 行で turn 開始時刻を記録する。
    tool_result の user 行 (= content が list で type=tool_result を含む) は除外。"""
    if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
        return
    content = (line.get("message") or {}).get("content")
    is_prompt = False
    if isinstance(content, str) and content.strip():
        is_prompt = True
    elif isinstance(content, list):
        is_prompt = any(
            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            for b in content
        )
    if not is_prompt:
        return
    ts = _parse_jsonl_timestamp(line.get("timestamp"))
    if ts is not None:
        _turn_started_at[session_id] = ts


def _attach_duration_to_result(session_id: str, line: dict, events: list[dict]) -> None:
    """assistant 行で確定 stop_reason の時、 (確定行 ts - turn 開始 ts) を duration_ms として
    events 内 result に in-place で乗せる。 開始が記録されてない (= backend 再起動跨ぎ等)
    なら何もしない。"""
    if line.get("type") != "assistant":
        return
    msg = line.get("message") or {}
    stop_reason = msg.get("stop_reason")
    if not stop_reason or stop_reason == "tool_use":
        return
    start = _turn_started_at.pop(session_id, None)
    if start is None:
        return
    end = _parse_jsonl_timestamp(line.get("timestamp"))
    if end is None:
        return
    duration_ms = max(0, int((end - start) * 1000))
    for ev in events:
        if ev.get("type") == "result":
            ev["duration_ms"] = duration_ms


def _mutate_agent_status(session_id: str, line: dict) -> bool:
    """JSONL 1 行から agent_status を更新する。 変化があれば True を返す
    (= caller が status_event.set() するための合図)。

    旧 sdk_runner._on_assistant_msg / _on_system_msg と同等の責務を JSONL 由来で果たす。
    PTY 経路では SDK の structured message が無いので、 JSONL の type/content から
    todos / plan_mode / current_tool / ctx_pct / model を直接拾う。
    """
    if not isinstance(line, dict) or line.get("isSidechain") or line.get("isMeta"):
        return False
    if session_id not in agent_status:
        return False
    a = agent_status[session_id]
    changed = False
    line_type = line.get("type")

    if line_type == "assistant":
        msg = line.get("message") or {}
        # model 表示用 (= StatusBar 5h/7d/ctx と並ぶ model 名)
        model_raw = msg.get("model")
        if model_raw:
            new_model = format_model_name(model_raw)
            if a.get("model") != new_model:
                a["model"] = new_model
                changed = True
        # usage → ctx_pct (= rate-limits.jsonl 由来とは別経路の保険)
        usage = msg.get("usage")
        if usage:
            ctx_window = a.get("ctx_window") or 1_000_000
            new_pct = compute_ctx_pct(usage, ctx_window)
            if a.get("ctx_pct") != new_pct:
                a["ctx_pct"] = new_pct
                changed = True
        # tool_use 解析: TodoWrite (進捗) / Enter|ExitPlanMode (plan_mode) / current_tool
        content = msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                tool_id = block.get("id")
                inp = block.get("input") or {}
                if name == "TodoWrite":
                    todos = inp.get("todos")
                    if todos is not None and a.get("todos") != todos:
                        a["todos"] = todos
                        changed = True
                elif name == "ExitPlanMode" and a.get("plan_mode"):
                    a["plan_mode"] = False
                    changed = True
                elif name == "EnterPlanMode" and not a.get("plan_mode"):
                    a["plan_mode"] = True
                    changed = True
                # current_tool: ActivityBar / 旧 SDK 経路と同型の「今走ってる tool」 情報
                a["current_tool"] = {
                    "name": name,
                    "id": tool_id,
                    "started_at": time.time(),
                }
                changed = True
        # stop_reason 確定 turn では current_tool を解放 (= 次 turn 開始まで空に)
        stop_reason = msg.get("stop_reason")
        if stop_reason and stop_reason != "tool_use":
            if a.get("current_tool") is not None:
                a["current_tool"] = None
                changed = True
    elif line_type == "user":
        # tool_result が来たら、 対応する current_tool が居れば解放
        msg = line.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                cur = a.get("current_tool")
                if cur and cur.get("id") == block.get("tool_use_id"):
                    a["current_tool"] = None
                    changed = True
    return changed


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
        evts = jsonl_line_to_events(obj)
        _attach_duration_to_result(session_id, obj, evts)
        for event in evts:
            frames.append(f"id: {pos}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
    if status_dirty and state is not None:
        state.status_event.set()
    return frames


def _initial_offset(path: Path) -> int:
    """初回 replay の開始バイト位置。 直近 INITIAL_REPLAY_LINES 行ぶんに絞る。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0
    if data.count(b"\n") <= INITIAL_REPLAY_LINES:
        return 0
    # 末尾から INITIAL_REPLAY_LINES 個の改行を遡った位置
    idx = len(data)
    remaining = INITIAL_REPLAY_LINES
    while remaining > 0:
        idx = data.rfind(b"\n", 0, idx)
        if idx == -1:
            return 0
        remaining -= 1
    return idx + 1


async def _jsonl_sse(session_id: str, start_pos: int | None = None):
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

    # tail: 新規追記行を追従する
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            size = path.stat().st_size
        except OSError:
            # ファイルが消えた (= セッション破棄等) → 終了
            return
        if size < pos:
            # truncate / rotate された → 先頭から読み直す
            pos = 0
        if size > pos:
            lines, pos = _read_complete_lines(path, pos)
            frames = _lines_to_sse(lines, pos, session_id)
            if frames:
                for frame in frames:
                    yield frame
                continue
        yield ": keep-alive\n\n"


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
