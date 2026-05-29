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
