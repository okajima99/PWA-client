"""tmux pane 配下の claude プロセスを探索して jsonl_watcher に登録する。

tmux session が生成されてから子 zsh / wrapper / claude が立ち上がるまで時間差があるので、
polling で claude プロセス (pid / cwd / 起動時刻) を捕まえて binding 登録する。 探索手順:

  tmux pane PID → pgrep -P で子孫 BFS → ps comm が 'claude' → ps lstart で起動時刻 →
  lsof で cwd → jsonl_watcher.register_pending

pty_runner からの spawn 直後 + backend 再起動跨ぎ (= 既存 tmux session の再アタッチ) で
呼ばれる。 pty_runner との循環 import を避けるため `_run_tmux` は遅延 import する。
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path


async def register_claude_when_ready(
    session_id: str, max_wait: float = 8.0, interval: float = 0.5,
) -> None:
    """tmux pane の子 claude プロセスが立ち上がるのを polling で待ち、 jsonl_watcher に登録する。

    launch_alias 経由だと claude 起動まで 1-2 秒、 環境次第でもう少しかかる。
    `max_wait` 秒以内に claude プロセスが見つからなければ諦める (= 既存 zsh のみで claude
    起動しないケース等)。
    """
    import jsonl_watcher  # 循環 import 回避のため遅延 import
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(interval)
        for pane_pid in tmux_pane_pids(session_id):
            claude_pid = find_claude_descendant(pane_pid)
            if claude_pid is None:
                continue
            start_time = process_start_time(claude_pid)
            cwd = process_cwd(claude_pid)
            if start_time is None or cwd is None:
                continue
            jsonl_watcher.register_pending(session_id, claude_pid, cwd, start_time)
            return


def tmux_pane_pids(session_id: str) -> list[int]:
    """指定 PWA session の tmux session に属する pane の PID 一覧。"""
    # pty_runner との循環 import を避けるため遅延 import (= 関数の最初の呼出時のみ評価)
    from pty_runner import USE_TMUX_WRAP, _run_tmux, _tmux_session_name
    if not USE_TMUX_WRAP:
        return []
    r = _run_tmux("list-panes", "-t", _tmux_session_name(session_id), "-F", "#{pane_pid}", text=True)
    if r is None or r.returncode != 0:
        return []
    return [int(s) for s in r.stdout.split() if s.strip().isdigit()]


def find_claude_descendant(root_pid: int, max_depth: int = 6) -> int | None:
    """BFS で子孫プロセスを辿り、 ps の comm の basename が 'claude' のものを返す。"""
    queue: list[tuple[int, int]] = [(root_pid, 0)]
    while queue:
        pid, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for child_str in result.stdout.split():
            child_str = child_str.strip()
            if not child_str.isdigit():
                continue
            child_pid = int(child_str)
            try:
                ps = subprocess.run(
                    ["ps", "-p", str(child_pid), "-o", "comm="],
                    capture_output=True, text=True, timeout=2,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            comm = ps.stdout.strip()
            if comm and Path(comm).name == "claude":
                return child_pid
            queue.append((child_pid, depth + 1))
    return None


def process_start_time(pid: int) -> float | None:
    """`ps -o lstart=` で取得した起動時刻文字列を unix epoch に変換。"""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    s = result.stdout.strip()
    if not s:
        return None
    # macOS lstart 形式: "Sun May 24 20:24:00 2026"
    try:
        return time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def process_cwd(pid: int) -> str | None:
    """lsof で cwd エントリを取得。 macOS は /proc が無いので lsof 経由。"""
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.split("\n"):
        if line.startswith("n"):
            return line[1:]
    return None
