"""hooks_router の単体テスト。

POST /hooks/event の payload 種別ごとの分岐 (= Stop / Notification / 未対応) と、
session_id 逆引き (= cwd → PWA session) の挙動を確認する。 実際の push 配信は
broadcast_push をモックして引数だけ検証する。
"""
import pytest

import hooks_router


@pytest.fixture
def captured_pushes(monkeypatch):
    """broadcast_push 呼出を捕捉して配信を抑止する。"""
    calls: list[tuple[str, str, str]] = []

    async def fake_push(body, title, session_id):
        calls.append((body, title, session_id))

    monkeypatch.setattr(hooks_router, "broadcast_push", fake_push)
    return calls


@pytest.fixture
def fake_agents(monkeypatch, tmp_path):
    """テスト用 AGENTS dict + cwd を tmp_path 下に張る。"""
    cwd_path = tmp_path / "project"
    cwd_path.mkdir()
    fake = {"primary": {"cwd": str(cwd_path)}, "default": {"cwd": str(tmp_path / "other")}}
    monkeypatch.setattr(hooks_router, "AGENTS", fake)
    return {"cwd": str(cwd_path), "id": "primary"}


@pytest.fixture
def fake_tmux_map(monkeypatch, tmp_path):
    """tmux session map を tmp に作って、 PWA セッション扱いの claude_sid を登録する。
    PWA 経由判定 (= `_pwa_session_for_claude_sid`) を test 内で再現するため。"""
    map_dir = tmp_path / "tmux-session-map"
    map_dir.mkdir()
    monkeypatch.setattr(hooks_router, "_TMUX_MAP", map_dir)

    def register(pwa_sid: str, claude_sid: str) -> None:
        (map_dir / f"pwa-{pwa_sid}").write_text(claude_sid, encoding="utf-8")

    return register


def test_pwa_session_for_cwd_exact_match(fake_agents):
    """agent.cwd と同一の path → その agent の id を返す。"""
    assert hooks_router._pwa_session_for_cwd(fake_agents["cwd"]) == "primary"


def test_pwa_session_for_cwd_subdirectory(fake_agents, tmp_path):
    """agent.cwd 配下の sub-path も同一 agent に解決する (= claude が中で cd した場合)。"""
    sub = tmp_path / "project" / "deep" / "nested"
    sub.mkdir(parents=True)
    assert hooks_router._pwa_session_for_cwd(str(sub)) == "primary"


def test_pwa_session_for_cwd_no_match(fake_agents, tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    assert hooks_router._pwa_session_for_cwd(str(unrelated)) is None


def test_pwa_session_for_cwd_handles_none():
    assert hooks_router._pwa_session_for_cwd(None) is None
    assert hooks_router._pwa_session_for_cwd("") is None


def test_truncate_short_passes_through():
    assert hooks_router._truncate("hello") == "hello"


def test_truncate_long_appends_ellipsis():
    long = "x" * 200
    result = hooks_router._truncate(long, limit=10)
    assert result == "xxxxxxxxxx…"


def test_endpoint_stop_event_pushes_output(fake_agents, fake_tmux_map, captured_pushes):
    """Stop イベントは last_assistant_message を本文に push する。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_tmux_map("primary", "claude-internal-uuid")
    monkeypatch_app = FastAPI()
    monkeypatch_app.include_router(hooks_router.router)
    client = TestClient(monkeypatch_app)

    payload = {
        "hook_event_name": "Stop",
        "session_id": "claude-internal-uuid",
        "cwd": fake_agents["cwd"],
        "last_assistant_message": "task completed successfully",
    }
    response = client.post("/hooks/event", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "pushed": "Stop"}
    assert len(captured_pushes) == 1
    body, title, sid = captured_pushes[0]
    assert "task completed successfully" in body
    assert sid == "primary"


def test_endpoint_notification_event_pushes_message(fake_agents, fake_tmux_map, captured_pushes):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_tmux_map("primary", "claude-internal-uuid")
    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    payload = {
        "hook_event_name": "Notification",
        "session_id": "claude-internal-uuid",
        "cwd": fake_agents["cwd"],
        "message": "permission needed",
        "type": "permission_prompt",
    }
    response = client.post("/hooks/event", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "pushed": "Notification"}
    assert captured_pushes[0][0] == "permission needed"


def test_endpoint_unknown_event_acks_without_push(fake_agents, fake_tmux_map, captured_pushes):
    """未対応イベントは受信 ack のみ、 push しない。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_tmux_map("primary", "claude-internal-uuid")
    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    payload = {
        "hook_event_name": "FileChanged",
        "session_id": "claude-internal-uuid",
        "cwd": fake_agents["cwd"],
        "file_path": "/foo",
    }
    response = client.post("/hooks/event", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": "FileChanged"}
    assert captured_pushes == []


def test_endpoint_non_pwa_session_is_ignored(fake_agents, fake_tmux_map, captured_pushes):
    """tmux_session_map に登録されてない claude_sid (= デスクトップ公式 / ターミナル直叩き)
    は ignored で push されないこと。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # fake_tmux_map で primary を登録しないので claude_sid は孤立
    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    payload = {
        "hook_event_name": "Stop",
        "session_id": "desktop-claude-uuid",
        "cwd": fake_agents["cwd"],
        "last_assistant_message": "this should not reach Web Push",
    }
    response = client.post("/hooks/event", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": "non_pwa_session"}
    assert captured_pushes == []


def test_endpoint_invalid_json_returns_400_payload(captured_pushes):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    response = client.post(
        "/hooks/event",
        content=b"this is not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "invalid_json"}
    assert captured_pushes == []
