"""state.py の pure 関数の unit test。 第一弾は _default_title のみ。"""
import state


def test_default_title_uses_display_name(monkeypatch):
    # 意図: AGENTS[id].display_name があればそれを base にして "<base>-<n>"
    monkeypatch.setitem(state.AGENTS, "_test_agent", {"display_name": "Fake"})
    assert state._default_title("_test_agent", 3) == "Fake-3"


def test_default_title_falls_back_to_upper(monkeypatch):
    # 意図: display_name 未定義は agent_id.upper() を base にする
    monkeypatch.setitem(state.AGENTS, "_test_tiny", {})
    assert state._default_title("_test_tiny", 1) == "_TEST_TINY-1"


def test_default_title_unknown_agent_id():
    # 意図: AGENTS に無い id でも upper fallback (= migration 中の保険)
    assert state._default_title("_ghost_agent", 7) == "_GHOST_AGENT-7"
