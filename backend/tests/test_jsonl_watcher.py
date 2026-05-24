"""jsonl_watcher の binding self-heal の unit test。

再 attach / backend restart の race で in-mem binding の jsonl_path が失われても、
SessionStart hook / persist 由来の確定 path (_confirmed_paths) が生きていれば
get_jsonl_for が self-heal して chat tail を復旧できることを固定する (= あるタブの chat が
急に読めなくなる事象の再発防止)。 _confirmed_paths は PWA_SID 確定のみが入るので、
同 cwd の別 claude プロセス (= デスクトップアプリ等) の混入は構造的に起きない。

注意: confirm_bind は _save_bindings() で _PERSIST_PATH に書き込む副作用を持つ。 テストでは
本番の logs/jsonl_bindings.json を壊さないよう、 autouse fixture で _PERSIST_PATH を tmp に
逃がし、 module-level の _bindings / _confirmed_paths を毎テスト clear する。
"""
import pytest

import jsonl_watcher as jw


@pytest.fixture(autouse=True)
def isolate_watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(jw, "_PERSIST_PATH", tmp_path / "bindings.json")
    jw._bindings.clear()
    jw._confirmed_paths.clear()
    yield
    jw._bindings.clear()
    jw._confirmed_paths.clear()


def test_confirm_bind_then_get(tmp_path):
    f = tmp_path / "abc.jsonl"
    f.write_text("{}\n")
    jw.confirm_bind("ses_1", "claude_1", str(f))
    assert jw.get_jsonl_for("ses_1") == f


def test_self_heal_when_inmem_binding_lost(tmp_path):
    # 確定後に in-mem binding が null 化 (= 再 attach race を模す) しても、
    # _confirmed_paths から復元して返すこと。
    f = tmp_path / "tab.jsonl"
    f.write_text("{}\n")
    jw.confirm_bind("ses_tab", "claude_tab", str(f))
    jw._bindings["ses_tab"].jsonl_path = None
    jw._bindings["ses_tab"].confirmed = False
    assert jw.get_jsonl_for("ses_tab") == f
    assert jw._bindings["ses_tab"].jsonl_path == f
    assert jw._bindings["ses_tab"].confirmed is True


def test_self_heal_when_binding_entry_gone(tmp_path):
    # _bindings entry ごと消えても _confirmed_paths から復元する。
    f = tmp_path / "x.jsonl"
    f.write_text("{}\n")
    jw.confirm_bind("ses_2", "claude_2", str(f))
    jw._bindings.pop("ses_2")
    assert jw.get_jsonl_for("ses_2") == f


def test_no_heal_when_confirmed_file_missing(tmp_path):
    # 確定 path のファイルが消えていれば None (= 存在しない物を bind しない)。
    jw._confirmed_paths["ses_3"] = tmp_path / "gone.jsonl"
    assert jw.get_jsonl_for("ses_3") is None


def test_confirm_bind_detaches_stale_and_blocks_reheal(tmp_path):
    # 同 path を持つ別 binding を confirm_bind が剥がし、 self-heal で復帰させないこと
    # (= 1 JSONL が 2 タブに流れる cross-contamination 防止)。
    f = tmp_path / "shared.jsonl"
    f.write_text("{}\n")
    jw.confirm_bind("ses_old", "c_old", str(f))
    jw.confirm_bind("ses_new", "c_new", str(f))
    assert jw.get_jsonl_for("ses_new") == f
    assert jw.get_jsonl_for("ses_old") is None
