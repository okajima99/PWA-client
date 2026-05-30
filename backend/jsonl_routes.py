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

import re

from jsonl_events import jsonl_line_to_events
from push import broadcast_push, notification_title_for
from pty_runner import capture_tmux_scrollback, jsonl_path_for_session
from state import agent_status, sessions_overview_event, stream_states
from usage import compute_ctx_pct, format_model_name


# ANSI escape を剥がして plain text にする (= tmux capture-pane の出力に色 / cursor 制御が
# 含まれる、 選択肢抽出時にノイズ)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# 「1. Yes, auto-accept edits」 みたいな choice 行を拾う
_PLAN_CHOICE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", re.MULTILINE)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


async def _capture_plan_choices(session_id: str, tool_use_id: str) -> None:
    """ExitPlanMode tool_use 直後に tmux 画面を capture して選択肢テキストを抽出する。
    claude TUI は tool_use を JSONL に書いた直後に terminal に prompt を描画するので、
    数百 ms 待ってから capture することで「1. Yes, ... / 2. ... / 3. ...」 が画面に
    出てる状態を拾える。

    抽出失敗時は agent_status.pending_plan.choices = [] で frontend が fallback の
    固定 2 択 (1=Approve / 3=No) を出す。
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


def _read_tail(path: Path, pos: int) -> tuple[list[str], int, str]:
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
    lines, new_pos = _read_complete_lines(path, pos)
    return lines, new_pos, "ok"


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


def _is_user_prompt(line: dict) -> bool:
    """素プロンプト (= 実ユーザ発言の user 行) か。 tool_result の user 行 (= content が
    list で type=tool_result) や isMeta / isSidechain は除外する。"""
    if line.get("type") != "user" or line.get("isSidechain") or line.get("isMeta"):
        return False
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            for b in content
        )
    return False


def _track_turn_start(session_id: str, line: dict) -> None:
    """素プロンプト (= ユーザ発言) の user 行で turn 開始時刻を記録する。"""
    if not _is_user_prompt(line):
        return
    ts = _parse_jsonl_timestamp(line.get("timestamp"))
    if ts is not None:
        _turn_started_at[session_id] = ts


def _update_busy(session_id: str, line: dict) -> None:
    """JSONL 1 行から session の busy (= turn 進行中か) を更新する。 変化したら
    sessions_overview_event を叩いて /sessions/status/stream に push させる。

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
    elif _is_user_prompt(line):
        new = True
    if new != st.busy:
        st.busy = new
        sessions_overview_event.set()


def _compute_busy_from_tail(path: Path, tail_bytes: int = 32768) -> bool:
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
        elif _is_user_prompt(d):
            return True
    return False


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


# 推論が止まる原因として通知すべき stop_reason → ユーザ向けラベル。 旧 SDK 経路で
# StopReasonChip として MessageItem に表示してたのと同じ集合 + tool_use は除外
# (= turn 継続中なので止まりではない)。
_STOP_REASON_NOTIF_LABELS = {
    "max_tokens": "⚠ トークン上限で停止",
    "refusal": "🚫 拒否されました",
    "pause_turn": "⏸ 一時停止",
    "model_context_window_exceeded": "⚠ コンテキスト窓超過",
}

# JSONL 行の `timestamp` (ISO 8601) が現在時刻から N 秒以内なら「新着 tail」 とみなす。
# 初回 replay (= 500 行) で過去行を読み返した時に古い AskUserQuestion / stop_reason
# 異常を再通知しないための gate。
_PUSH_FRESH_WINDOW_SEC = 60.0


def _is_fresh_line(line: dict) -> bool:
    """line の timestamp が直近 _PUSH_FRESH_WINDOW_SEC 内ならば True。"""
    ts = line.get("timestamp")
    if not ts or not isinstance(ts, str):
        return False
    try:
        line_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return False
    return (time.time() - line_time) < _PUSH_FRESH_WINDOW_SEC


def _maybe_push_blockers(session_id: str, line: dict) -> None:
    """推論を止める要因 (= AskUserQuestion 発火 / stop_reason 異常) を JSONL 上で検出して
    Web Push に流す。 旧 SDK 経路の make_permission_handler / _on_result_msg の役割を
    PTY/JSONL 経路で再現する箇所。

    - AskUserQuestion: assistant 行の tool_use(name="AskUserQuestion") を見つけたら
      質問本文を通知に乗せる (= 旧 SDK の AskUserQuestion 通知と同 spec)。
    - stop_reason 異常系 (max_tokens / refusal / pause_turn /
      model_context_window_exceeded): label を通知に乗せる。 end_turn / tool_use は
      正常系なので除外 (= turn 完了の通知は Stop hook 経路で別に飛ぶ)。

    初回 replay で過去行が再流入した時の再通知を防ぐため、 `_is_fresh_line` で
    タイムスタンプが直近 60 秒以内の行のみ push 発火する。
    """
    if line.get("type") != "assistant" or line.get("isSidechain") or line.get("isMeta"):
        return
    if not _is_fresh_line(line):
        return
    msg = line.get("message") or {}

    # AskUserQuestion 発火
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "AskUserQuestion":
                continue
            inp = block.get("input") or {}
            questions = inp.get("questions") or []
            first_q = questions[0] if isinstance(questions, list) and questions else {}
            question_text = (
                first_q.get("question") if isinstance(first_q, dict) else None
            )
            if question_text:
                title = notification_title_for(session_id)
                asyncio.create_task(
                    broadcast_push(f"❓ {question_text}", title, session_id)
                )
                return  # 1 行から複数 push を発火させない

    # stop_reason 異常系
    stop_reason = msg.get("stop_reason")
    label = _STOP_REASON_NOTIF_LABELS.get(stop_reason)
    if label:
        title = notification_title_for(session_id)
        asyncio.create_task(broadcast_push(label, title, session_id))


# --- subagent 進行中の表示 (= 0-6) ---
# Task tool 実行中、 claude は各サブエージェントの transcript をメイン JSONL ではなく
# <jsonl>/<session-id>/subagents/agent-<id>.jsonl に別ファイルで書く (= v2.1.x 形式、
# メイン JSONL に sidechain 行は来ない)。 その最新ファイルの最後の tool_use 名を拾って
# 「↳ Read」 等と Task 行に inline 表示する。 並列サブエージェント時は mtime 最新の 1 つ
# だけを単一値で出す (= 割り切り、 frontend は status.subagent.last_tool を単一読み)。
_SUBAGENT_TAIL_BYTES = 65536


def _latest_subagent_tool(jsonl_path: Path, since: float) -> str | None:
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


def _refresh_subagent_status(session_id: str, jsonl_path: Path) -> bool:
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
    name = _latest_subagent_tool(jsonl_path, cur.get("started_at") or 0)
    new_val = {"last_tool": name} if name else None
    if a.get("subagent") != new_val:
        a["subagent"] = new_val
        return True
    return False


def _mutate_agent_status(session_id: str, line: dict) -> bool:
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
                    asyncio.create_task(_capture_plan_choices(session_id, tool_id))
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
