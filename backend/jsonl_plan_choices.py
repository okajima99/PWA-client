"""ExitPlanMode の承認選択肢を tmux 画面から抽出する。

claude TUI は ExitPlanMode tool_use を JSONL に書いた直後に terminal に承認 prompt
(「1. Yes, ... / 2. ... / 3. ...」) を描画するので、 数百 ms 待ってから tmux capture-pane
で拾うことで PWA 側で PlanApprovalBubble を構築できる (= 抽出失敗時は frontend 側の
fallback で固定 2 択にフォールバック)。
"""
from __future__ import annotations

import asyncio
import re

from pty_runner import capture_tmux_scrollback
from state import agent_status, stream_states


# ANSI escape を剥がして plain text にする (= tmux capture-pane の出力に色 / cursor 制御が
# 含まれる、 選択肢抽出時にノイズ)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# 「1. Yes, auto-accept edits」 みたいな choice 行を拾う
_PLAN_CHOICE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", re.MULTILINE)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


async def capture_plan_choices(session_id: str, tool_use_id: str) -> None:
    """ExitPlanMode tool_use 直後に tmux 画面を capture して選択肢テキストを抽出する。

    抽出失敗時は agent_status.pending_plan.choices = [] のまま (frontend が fallback の
    固定 2 択 (1=Approve / 3=No) を出す)。
    """
    await asyncio.sleep(0.5)
    a = agent_status.get(session_id)
    if a is None:
        return
    pending = a.get("pending_plan")
    if not pending or pending.get("tool_use_id") != tool_use_id:
        return  # 既に resolved or 別 plan に上書き
    try:
        raw = capture_tmux_scrollback(session_id, lines=120)
    except Exception:
        raw = b""
    if not raw:
        return
    text = _strip_ansi(raw.decode("utf-8", errors="replace"))
    # 直近の choice 行を抽出 (= 末尾近くにある番号付き行)
    choices = []
    seen_keys = set()
    for m in _PLAN_CHOICE_RE.finditer(text):
        key, label = m.group(1), m.group(2)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # label の末尾に「 (esc to interrupt)」 等の補助文言が混ざる場合があるので捨てる
        label = label.split("(")[0].strip()
        if label:
            choices.append({"key": key, "label": label})
    # 「1. ... / 2. ... / 3. ...」 のように **連続した番号**だけ採用 (= 過去画面の番号付き
    # リストが混ざるのを防ぐ)。 末尾近くから連続な keys を取る
    if len(choices) >= 2:
        # 末尾から「N, N-1, N-2 ...」 と降順で連続するブロックを抽出
        tail = []
        for c in reversed(choices):
            if not tail:
                tail.append(c)
                continue
            prev_key = int(tail[-1]["key"])
            if int(c["key"]) == prev_key - 1:
                tail.append(c)
            else:
                break
        tail.reverse()
        choices = tail

    # state が他に上書きされてないか再確認 → set
    pending = a.get("pending_plan")
    if pending and pending.get("tool_use_id") == tool_use_id:
        a["pending_plan"] = {**pending, "choices": choices}
        state = stream_states.get(session_id)
        if state is not None:
            state.status_event.set()
