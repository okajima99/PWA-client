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
