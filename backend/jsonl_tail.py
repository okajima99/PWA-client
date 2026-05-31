"""JSONL ファイル tail の低レベルプリミティブ。

SSE 配信 (`jsonl_routes._jsonl_sse`) と push 監視 (`monitor_all_sessions_loop`)
が共有する純粋関数群。 backend mem state には触らず、 path + offset → 行リスト
の関数だけを持つ。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def parse_jsonl_timestamp(ts: str | None) -> float | None:
    """JSONL 行の `timestamp` (= ISO 8601 "Z" 終端) を unix epoch に変換。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def read_complete_lines(path: Path, pos: int) -> tuple[list[str], int]:
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


def read_tail(path: Path, pos: int) -> tuple[list[str], int, str]:
    """path を pos から tail する共通プリミティブ (= SSE 配信 / push 監視で共用)。

    返り値 (lines, new_pos, status):
      - "ok"        : 新規完全行あり (lines / new_pos が進む)
      - "nochange"  : 新着なし (new_pos == pos)
      - "truncated" : size < pos (= rotate / truncate。 new_pos = 現 size)
      - "error"     : stat 失敗 (= ファイル消失等)
    truncate 後にどこから読み直すかは呼び側の方針 (= SSE は先頭再生、 monitor は末尾再同期)。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], pos, "error"
    if size < pos:
        return [], size, "truncated"
    if size <= pos:
        return [], pos, "nochange"
    lines, new_pos = read_complete_lines(path, pos)
    return lines, new_pos, "ok"
