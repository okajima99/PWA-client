"""jsonl_routes.py の tail 読み取りプリミティブの unit test。

`_read_complete_lines` / `_read_tail` / `_initial_offset` は SSE 配信と push 監視の
両方が依存する subtle なファイル tail ロジック (= 部分行の持ち越し、 truncate 検知、
初回 replay の行絞り)。 ファイルだけで完結する純粋関数なので fixture は tmp_path のみ。
"""
import jsonl_routes as jr


# ---------------------------------------------------------------------------
# _read_complete_lines: 改行で終わる完全行だけ返し、 末尾の部分行は次回に持ち越す
# ---------------------------------------------------------------------------

def test_read_complete_lines_full(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\nb\n")
    assert jr._read_complete_lines(p, 0) == (["a", "b"], 4)


def test_read_complete_lines_partial_tail_held_back(tmp_path):
    # 末尾 "b" は \n が無い = 書き込み途中。 pos は最後の完全行直後 (= 2) までしか進めない
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\nb")
    assert jr._read_complete_lines(p, 0) == (["a"], 2)


def test_read_complete_lines_no_new(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\n")
    assert jr._read_complete_lines(p, 2) == ([], 2)


def test_read_complete_lines_missing_file(tmp_path):
    assert jr._read_complete_lines(tmp_path / "nope.jsonl", 0) == ([], 0)


# ---------------------------------------------------------------------------
# _read_tail: (lines, new_pos, status) — ok / nochange / truncated / error
# ---------------------------------------------------------------------------

def test_read_tail_ok(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\nb\n")
    assert jr._read_tail(p, 0) == (["a", "b"], 4, "ok")


def test_read_tail_nochange(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\nb\n")
    assert jr._read_tail(p, 4) == ([], 4, "nochange")


def test_read_tail_partial(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\nb")  # b は未確定
    assert jr._read_tail(p, 0) == (["a"], 2, "ok")


def test_read_tail_truncated_resyncs_to_size(tmp_path):
    # pos がファイルサイズを超える (= rotate / truncate) → new_pos = 現 size、 status=truncated
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"a\n")
    assert jr._read_tail(p, 999) == ([], 2, "truncated")


def test_read_tail_error_on_missing(tmp_path):
    assert jr._read_tail(tmp_path / "nope.jsonl", 5) == ([], 5, "error")


# ---------------------------------------------------------------------------
# _initial_offset: 直近 INITIAL_REPLAY_LINES 行に絞る (= 末尾 seek、 全読みしない)
# ---------------------------------------------------------------------------

def test_initial_offset_small_file_returns_zero(tmp_path):
    # 改行が INITIAL_REPLAY_LINES 以下 → 全件 replay (= 0)
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"".join(f"L{i}\n".encode() for i in range(10)))
    assert jr._initial_offset(p) == 0


def test_initial_offset_boundary_equals_n(tmp_path):
    # ちょうど N 行 = 全件 (= count <= N → 0)、 旧実装と同じ境界
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"".join(f"L{i}\n".encode() for i in range(jr.INITIAL_REPLAY_LINES)))
    assert jr._initial_offset(p) == 0


def test_initial_offset_large_file_keeps_last_n(tmp_path):
    n = jr.INITIAL_REPLAY_LINES
    total = n + 100
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"".join(f"L{i}\n".encode() for i in range(total)))
    off = jr._initial_offset(p)
    assert off > 0
    # 「末尾から N 個目の改行の直後」 を返す = 末尾 N-1 行ぶん。 旧実装 (全読み + rfind) と
    # 同じ off-by-one を踏襲しており、 初回 replay の行数キャップとしては実害なし。
    lines, _ = jr._read_complete_lines(p, off)
    assert len(lines) == n - 1
    assert lines[0] == f"L{total - (n - 1)}"
    assert lines[-1] == f"L{total - 1}"


def test_initial_offset_empty_file(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"")
    assert jr._initial_offset(p) == 0


# ---------------------------------------------------------------------------
# _latest_subagent_tool / _refresh_subagent_status: Task 実行中のサブツール名抽出 (= 0-6)
# subagent transcript は <jsonl>/<sid>/subagents/agent-*.jsonl に別ファイルで書かれる
# ---------------------------------------------------------------------------
import json
import os


def _write_agent_file(subdir, name, tools, mtime=None):
    """subagents/<name>.jsonl に assistant tool_use 行を時系列で書く helper。"""
    subdir.mkdir(parents=True, exist_ok=True)
    p = subdir / name
    lines = []
    for t in tools:
        lines.append(json.dumps({
            "type": "assistant",
            "isSidechain": True,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": t}]},
        }))
    p.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# --- busy 判定 (案B: 全session 状態の backend 権威ソース) ---

def _asst(stop_reason):
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop_reason,
                                             "content": [{"type": "text", "text": "x"}]}}

def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def test_is_user_prompt_true_for_real_text():
    assert jr._is_user_prompt(_user("hello")) is True
    assert jr._is_user_prompt({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}) is True


def test_is_user_prompt_false_for_harness_xml():
    """claude TUI が user 行として書く harness XML (slash command / shell stdout 等) を
    ユーザ発話扱いしない (= ターミナルで /clear や ls 等を打っただけで busy が立つ事象を防ぐ)。"""
    # 文字列 content (slash command)
    assert jr._is_user_prompt(_user("<command-name>/clear</command-name>")) is False
    assert jr._is_user_prompt(_user("<command-message>clear</command-message>")) is False
    assert jr._is_user_prompt(_user("<command-args>sonnet</command-args>")) is False
    assert jr._is_user_prompt(_user("<local-command-stdout>foo</local-command-stdout>")) is False
    assert jr._is_user_prompt(_user("<local-command-stderr>err</local-command-stderr>")) is False
    # list content (text block で同じ XML)
    line = {"type": "user", "message": {"content": [{"type": "text", "text": "<command-name>/clear</command-name>"}]}}
    assert jr._is_user_prompt(line) is False


def test_is_user_prompt_false_for_tool_result_and_meta():
    # tool_result の user 行 (content が list で text 無し) は除外
    assert jr._is_user_prompt({"type": "user", "message": {"content": [{"type": "tool_result", "content": "r"}]}}) is False
    assert jr._is_user_prompt({"type": "user", "isMeta": True, "message": {"content": "x"}}) is False
    assert jr._is_user_prompt({"type": "user", "isSidechain": True, "message": {"content": "x"}}) is False
    assert jr._is_user_prompt(_asst("end_turn")) is False


def test_update_busy_transitions(isolated_state):
    state = isolated_state
    import state as state_mod
    sid = "ses_busy"
    state.stream_states[sid] = state_mod.StreamState(agent_id="a")
    ev = state_mod.sessions_overview_event
    ev.clear()

    # 素ユーザ発話 → busy=True + event set
    jr._update_busy(sid, _user("go"))
    assert state.stream_states[sid].busy is True
    assert ev.is_set() is True
    ev.clear()

    # tool_use 継続 → busy=True 維持 (変化なし → event は set されない)
    jr._update_busy(sid, _asst("tool_use"))
    assert state.stream_states[sid].busy is True
    assert ev.is_set() is False

    # end_turn → busy=False + event set
    jr._update_busy(sid, _asst("end_turn"))
    assert state.stream_states[sid].busy is False
    assert ev.is_set() is True


def test_update_busy_refusal_completes(isolated_state):
    state = isolated_state
    import state as state_mod
    sid = "ses_ref"
    state.stream_states[sid] = state_mod.StreamState(agent_id="a", busy=True)
    jr._update_busy(sid, _asst("refusal"))
    assert state.stream_states[sid].busy is False


def test_update_busy_unknown_session_noop():
    # 登録されてない sid は黙って無視 (例外を投げない)
    jr._update_busy("__no_such_sid__", _user("x"))


def test_compute_busy_from_tail(tmp_path):
    p = tmp_path / "s.jsonl"
    # 末尾が tool_use → busy=True
    p.write_text("\n".join(json.dumps(x) for x in [_user("go"), _asst("tool_use")]) + "\n")
    assert jr._compute_busy_from_tail(p) is True
    # 末尾が end_turn → busy=False
    p.write_text("\n".join(json.dumps(x) for x in [_user("go"), _asst("tool_use"), _asst("end_turn")]) + "\n")
    assert jr._compute_busy_from_tail(p) is False
    # 素ユーザ発話だけ (assistant 未着) → busy=True
    p.write_text(json.dumps(_user("go")) + "\n")
    assert jr._compute_busy_from_tail(p) is True


def test_compute_busy_from_tail_missing_file(tmp_path):
    assert jr._compute_busy_from_tail(tmp_path / "nope.jsonl") is False


def test_latest_subagent_tool_returns_last_tool(tmp_path):
    jsonl = tmp_path / "ses1.jsonl"
    sub = tmp_path / "ses1" / "subagents"
    _write_agent_file(sub, "agent-a.jsonl", ["Read", "Write", "Bash"], mtime=1000)
    assert jr._latest_subagent_tool(jsonl, since=0) == "Bash"


def test_latest_subagent_tool_picks_newest_file(tmp_path):
    jsonl = tmp_path / "ses1.jsonl"
    sub = tmp_path / "ses1" / "subagents"
    _write_agent_file(sub, "agent-old.jsonl", ["Read"], mtime=1000)
    _write_agent_file(sub, "agent-new.jsonl", ["Grep"], mtime=2000)
    assert jr._latest_subagent_tool(jsonl, since=0) == "Grep"


def test_latest_subagent_tool_filters_by_since(tmp_path):
    # since (= 現 Task の started_at) より前に書かれた古い agent ファイルは無視する
    jsonl = tmp_path / "ses1.jsonl"
    sub = tmp_path / "ses1" / "subagents"
    _write_agent_file(sub, "agent-stale.jsonl", ["Read"], mtime=500)
    assert jr._latest_subagent_tool(jsonl, since=1000) is None


def test_latest_subagent_tool_no_dir(tmp_path):
    assert jr._latest_subagent_tool(tmp_path / "nope.jsonl", since=0) is None


def test_refresh_subagent_sets_and_clears(tmp_path, isolated_state):
    state = isolated_state
    sid = "ses_test"
    state.agent_status[sid] = {"current_tool": {"name": "Task", "started_at": 0}, "subagent": None}
    jsonl = tmp_path / "ses_test.jsonl"
    sub = tmp_path / "ses_test" / "subagents"
    _write_agent_file(sub, "agent-a.jsonl", ["Read"], mtime=1000)
    # Task 実行中 → last_tool が立つ
    assert jr._refresh_subagent_status(sid, jsonl) is True
    assert state.agent_status[sid]["subagent"] == {"last_tool": "Read"}
    # 変化なし → False
    assert jr._refresh_subagent_status(sid, jsonl) is False
    # Task 終了 (current_tool が落ちる) → subagent も落ちる
    state.agent_status[sid]["current_tool"] = None
    assert jr._refresh_subagent_status(sid, jsonl) is True
    assert state.agent_status[sid]["subagent"] is None
