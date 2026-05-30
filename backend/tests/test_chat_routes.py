"""chat_routes.py の require_session 依存の unit test。

各 session 系 endpoint が共有する 404 ガードを 1 箇所に集約したもの。 存在すれば
session_id をそのまま返し、 無ければ HTTPException(404) を投げる。
"""
import asyncio

import pytest
from fastapi import HTTPException


def _setup_session(state, sid="ses_cfg"):
    from state import StreamState
    state.sessions_meta[sid] = object()
    state.stream_states[sid] = StreamState()
    return sid


def test_build_sessions_overview_reflects_busy(isolated_state):
    """全session overview payload が各 session の busy / pending_question を反映する (= 案B)。"""
    import chat_routes
    from state import StreamState
    state = isolated_state
    state.sessions_meta.clear()
    state.stream_states.clear()
    state.agent_status.clear()
    # busy=True の session と busy=False の session
    state.sessions_meta["ses_a"] = object()
    state.sessions_meta["ses_b"] = object()
    state.stream_states["ses_a"] = StreamState(busy=True)
    state.stream_states["ses_b"] = StreamState(busy=False)
    state.agent_status["ses_a"] = {"pending_question": None}
    state.agent_status["ses_b"] = {"pending_question": {"questions": []}}

    ov = chat_routes._build_sessions_overview()
    assert ov["ses_a"] == {"busy": True, "pending_question": False}
    assert ov["ses_b"] == {"busy": False, "pending_question": True}


def test_patch_config_fast_toggle_sends_once_on_change(isolated_state, monkeypatch):
    """`/fast` はトグルなので、 希望状態が現状と変わった時だけ 1 回打鍵する。
    同値 PATCH では打鍵しない (= 2 連打で ON→OFF に戻る事故を防ぐ)。"""
    import chat_routes  # noqa: F401
    import pty_runner

    sent = []
    monkeypatch.setattr(pty_runner, "tmux_send_keys",
                        lambda sid, **kw: sent.append((sid, kw)) or True)
    sid = _setup_session(isolated_state)

    res = asyncio.run(chat_routes.patch_session_config(sid, {"fast": True}, sid))
    assert res["fast"] is True
    assert sent == [(sid, {"text": "/fast", "enter": True})]

    sent.clear()
    res = asyncio.run(chat_routes.patch_session_config(sid, {"fast": True}, sid))
    assert res["fast"] is True
    assert sent == []  # 同値: 送らない

    res = asyncio.run(chat_routes.patch_session_config(sid, {"fast": False}, sid))
    assert res["fast"] is False
    assert sent == [(sid, {"text": "/fast", "enter": True})]  # OFF へ戻すで再送


def test_patch_config_accepts_auto_and_ultracode(isolated_state, monkeypatch):
    import chat_routes
    import pty_runner

    monkeypatch.setattr(pty_runner, "tmux_send_keys", lambda *a, **k: True)
    sid = _setup_session(isolated_state, "ses_eff")
    for e in ("auto", "ultracode"):
        res = asyncio.run(chat_routes.patch_session_config(sid, {"effort": e}, sid))
        assert res["effort"] == e


def test_patch_config_rejects_unknown_effort(isolated_state, monkeypatch):
    import chat_routes
    import pty_runner

    monkeypatch.setattr(pty_runner, "tmux_send_keys", lambda *a, **k: True)
    sid = _setup_session(isolated_state, "ses_eff2")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_routes.patch_session_config(sid, {"effort": "bogus"}, sid))
    assert exc.value.status_code == 400


def test_require_session_passes_for_known_id(isolated_state):
    import chat_routes
    import state

    sid = "ses_known"
    # require_session は membership だけ見る (= 値は何でもよい)
    state.sessions_meta[sid] = object()
    assert chat_routes.require_session(sid) == sid


def test_require_session_raises_404_for_unknown(isolated_state):
    import chat_routes

    with pytest.raises(HTTPException) as exc:
        chat_routes.require_session("ses_does_not_exist")
    assert exc.value.status_code == 404
