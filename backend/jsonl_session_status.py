"""JSONL 1 行から session の状態 (= busy / turn 開始 / agent_status / subagent) を更新する。

`jsonl_routes._lines_to_sse` (= SSE 配信) と `monitor_all_sessions_loop` (= 全 session
push 監視) の双方から呼ばれる「JSONL → backend state mutation」 を集約する場所。

主な責務:
- busy 判定 (StreamState.busy、 backend 権威 / overview SSE 経由で frontend loading を駆動)
- turn 開始時刻 (= duration_ms 算出のため)
- agent_status の todos / plan_mode / current_tool / ctx_pct / model / pending_plan /
  pending_question の更新
- subagent の last_tool 表示 (= Task 実行中の inline 進捗)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from jsonl_events import HARNESS_XML_RE
from jsonl_plan_choices import capture_plan_choices
from jsonl_tail import parse_jsonl_timestamp
from state import agent_status, sessions_overview_event, stream_states
from usage import compute_ctx_pct, format_model_name


# sid → 直近 user 発話 (= turn 開始) の unix epoch。 stop_reason 確定行を見たら
# (現在の確定行の timestamp - 開始) を duration_ms として result event に inject する。
# プロセス内 dict なので backend 再起動で消える、 中断中の turn は duration 取得不可。
_turn_started_at: dict[str, float] = {}


def is_user_prompt(line: dict) -> bool:
    """素プロンプト (= 実ユーザ発言の user 行) か。 tool_result の user 行 (= content が
    list で type=tool_result) や isMeta / isSidechain は除外する。

    さらに claude TUI が user 行として書く harness 内部表現
    (= `<command-name>/clear</command-name>` / `<local-command-stdout>...</local-command-stdout>`
    等。 ターミナルから slash command や shell コマンドを打った時に発生) も除外する。
    これらをユーザ発話扱いすると、 チャットに何も送ってないのに busy=True が立って停止ボタンが
    アクティブになる事象を引き起こす (2026-05-31 修正)。 harness XML 検出は jsonl_events と
    共通 (= 同じ regex を 2 箇所で持つと判定がズレるため)。"""
    if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
        return False
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return False
        return not HARNESS_XML_RE.match(s)
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "text":
                continue
            t = (b.get("text") or "").strip()
            if t and not HARNESS_XML_RE.match(t):
                return True
        return False
    return False


def track_turn_start(session_id: str, line: dict) -> None:
    """素プロンプト (= ユーザ発言) の user 行で turn 開始時刻を記録する。"""
    if not is_user_prompt(line):
        return
    ts = parse_jsonl_timestamp(line.get("timestamp"))
    if ts is not None:
        _turn_started_at[session_id] = ts


def update_busy(session_id: str, line: dict) -> None:
    """JSONL 1 行から session の busy (= turn 進行中か) を更新する。 変化したら
    sessions_overview_event を叩いて /sessions/overview/stream に push させる。

    素ユーザ発話 → busy=True (推論開始)。 assistant 行は stop_reason で判定:
    `tool_use` (= ツール継続中) は busy=True、 それ以外の確定 stop_reason
    (end_turn / max_tokens / refusal 等) は busy=False (= turn 完了)。 jsonl_events の
    result 合成と同一基準で、 SSE result 配信を経由しないので取りこぼさない。"""
    st = stream_states.get(session_id)
    if st is None:
        return
    new = st.busy
    if line.get("type") == "assistant":
        sr = (line.get("message") or {}).get("stop_reason")
        if sr == "tool_use":
            new = True
        elif sr:
            new = False
    elif is_user_prompt(line):
        new = True
    if new != st.busy:
        st.busy = new
        sessions_overview_event.set()


def compute_busy_from_tail(path: Path, tail_bytes: int = 32768) -> bool:
    """JSONL 末尾を読んで現在の busy を算出する (= monitor 初回 / path 切替時の初期化用)。
    後ろから最初に当たった確定シグナル (assistant の stop_reason or 素ユーザ発話) で決める。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return False
    for raw in reversed([ln for ln in data.split(b"\n") if ln.strip()]):
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("type") == "assistant":
            sr = (d.get("message") or {}).get("stop_reason")
            if sr == "tool_use":
                return True
            if sr:
                return False
        elif is_user_prompt(d):
            return True
    return False


def attach_duration_to_result(session_id: str, line: dict, events: list[dict]) -> None:
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
    end = parse_jsonl_timestamp(line.get("timestamp"))
    if end is None:
        return
    duration_ms = max(0, int((end - start) * 1000))
    for ev in events:
        if ev.get("type") == "result":
            ev["duration_ms"] = duration_ms


# --- subagent 進行中の表示 (= 0-6) ---
# Task tool 実行中、 claude は各サブエージェントの transcript をメイン JSONL ではなく
# <jsonl>/<session-id>/subagents/agent-<id>.jsonl に別ファイルで書く (= v2.1.x 形式、
# メイン JSONL に sidechain 行は来ない)。 その最新ファイルの最後の tool_use 名を拾って
# 「↳ Read」 等と Task 行に inline 表示する。 並列サブエージェント時は mtime 最新の 1 つ
# だけを単一値で出す (= 割り切り、 frontend は status.subagent.last_tool を単一読み)。
_SUBAGENT_TAIL_BYTES = 65536


def latest_subagent_tool(jsonl_path: Path, since: float) -> str | None:
    """jsonl_path 対応の subagents/ で mtime 最新かつ since 以降に更新された
    agent-*.jsonl を読み、 最後の assistant tool_use 名を返す。 無ければ None。

    since で絞るのは、 同一 session の subagents/ に過去 Task の古い agent ファイルが
    残るため (= 現 Task の started_at 以降に書かれたものだけを対象にして stale 表示を防ぐ)。
    """
    subdir = jsonl_path.parent / jsonl_path.stem / "subagents"
    try:
        candidates = [
            p for p in subdir.glob("agent-*.jsonl") if p.stat().st_mtime >= since
        ]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        size = newest.stat().st_size
        with open(newest, "rb") as f:
            # 毎 tick 全読みを避け、 末尾チャンクだけ読む (= 最後の tool_use が末尾近くに居る)。
            if size > _SUBAGENT_TAIL_BYTES:
                f.seek(size - _SUBAGENT_TAIL_BYTES)
                f.readline()  # seek 直後の途中行を捨てる
            data = f.read()
    except OSError:
        return None
    last_tool: str | None = None
    for raw in data.decode("utf-8", errors="replace").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name:
                    last_tool = name
    return last_tool


def refresh_subagent_status(session_id: str, jsonl_path: Path) -> bool:
    """current_tool が Task の間だけ subagent.last_tool を最新化する。 変化があれば True。

    Task 非実行中に subagent が残っていれば落とす (= tool_result / Stop hook clear の保険)。
    """
    a = agent_status.get(session_id)
    if a is None:
        return False
    cur = a.get("current_tool")
    if not (cur and cur.get("name") == "Task"):
        if a.get("subagent") is not None:
            a["subagent"] = None
            return True
        return False
    name = latest_subagent_tool(jsonl_path, cur.get("started_at") or 0)
    new_val = {"last_tool": name} if name else None
    if a.get("subagent") != new_val:
        a["subagent"] = new_val
        return True
    return False


def mutate_agent_status(session_id: str, line: dict) -> bool:
    """JSONL 1 行から agent_status を更新する。 変化があれば True を返す
    (= caller が status_event.set() するための合図)。

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
                elif name == "ExitPlanMode":
                    # plan_mode フラグは落とす (= 旧経路と同じ semantics)
                    if a.get("plan_mode"):
                        a["plan_mode"] = False
                        changed = True
                    # 承認待ち状態を立てる → frontend が PlanApprovalBubble を表示する
                    a["pending_plan"] = {
                        "tool_use_id": tool_id,
                        "plan": inp.get("plan", ""),
                        "choices": [],  # 0.5s 後に tmux capture-pane で抽出
                    }
                    changed = True
                    # 選択肢抽出は async タスクで遅延実行 (= claude TUI の prompt 描画待ち)
                    asyncio.create_task(capture_plan_choices(session_id, tool_id))
                elif name == "EnterPlanMode" and not a.get("plan_mode"):
                    a["plan_mode"] = True
                    changed = True
                elif name == "AskUserQuestion":
                    # PreToolUse hook で先に立てた pending_question に、 JSONL 由来の
                    # tool_use_id を補完する (= hook payload には id が無い)。 これで
                    # 回答 tool_result との突合 (= clear 判定) ができる。
                    pq = a.get("pending_question")
                    if pq is not None and pq.get("tool_use_id") is None:
                        a["pending_question"] = {**pq, "tool_use_id": tool_id}
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
                tu_id = block.get("tool_use_id")
                cur = a.get("current_tool")
                if cur and cur.get("id") == tu_id:
                    a["current_tool"] = None
                    changed = True
                # ExitPlanMode の承認 / 拒否が tool_result で返ったら pending_plan を解除
                pending = a.get("pending_plan")
                if pending and pending.get("tool_use_id") == tu_id:
                    a["pending_plan"] = None
                    changed = True
                # AskUserQuestion の回答が tool_result で返ったら pending_question を解除
                # (= ライブ overlay を消す。 以降は JSONL 由来の回答済みバブルが chat に残る)
                pq = a.get("pending_question")
                if pq and pq.get("tool_use_id") == tu_id:
                    a["pending_question"] = None
                    changed = True
    return changed
