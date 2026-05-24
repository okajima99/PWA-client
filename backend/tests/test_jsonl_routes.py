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
