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

from config import TMUX_SESSION_MAP_DIR
from jsonl_events import jsonl_line_to_events
from pty_routes import _resolve_cwd
from pty_runner import _tmux_session_name

logger = logging.getLogger(__name__)

router = APIRouter()

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# statusline が「tmux session 名 → claude session id」 を 1 session = 1 ファイルで書き出す
# ディレクトリ (= config 経由)。 複数タブが同じ cwd を共有しても、 これで JSONL を一意に
# 特定できる (= 単純な最新 mtime だと別タブの会話が混入する)。 未設定なら None。
TMUX_SESSION_MAP = Path(TMUX_SESSION_MAP_DIR).expanduser() if TMUX_SESSION_MAP_DIR else None

# 初回接続時に遡って replay する最大行数 (= 長い履歴で初回ペイロードが膨らむのを防ぐ)。
INITIAL_REPLAY_LINES = 2000

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


def _lines_to_sse(lines: list[str], pos: int) -> list[str]:
    """JSONL 行 (文字列) のリストを SSE フレームのリストに変換する。

    各フレームに `id: <pos>` (= この行群を読み終えた後のバイト位置) を付ける。 EventSource は
    受信した最後の id を保持し、 再接続時に `Last-Event-ID` ヘッダで送るので、 backend は
    そこから続きだけ流せる (= backend 再起動後の全 replay を回避)。
    """
    frames: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for event in jsonl_line_to_events(obj):
            frames.append(f"id: {pos}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
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
    for frame in _lines_to_sse(lines, pos):
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
            frames = _lines_to_sse(lines, pos)
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
