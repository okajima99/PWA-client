"""hooks_router の単体テスト。

POST /hooks/event の payload 種別ごとの分岐 (= Stop / Notification / 未対応) と、
session_id 逆引き (= cwd → PWA session) の挙動を確認する。 実際の push 配信は
broadcast_push をモックして引数だけ検証する。
"""
from pathlib import Path

import pytest

import hooks_router
import jsonl_watcher


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
def fake_bindings(monkeypatch, tmp_path):
    """jsonl_watcher._bindings に confirmed binding を直接張る fixture。
    f5d0ca6 以降の `_pwa_session_for_claude_sid` は statusline map ではなく
    `jsonl_watcher.list_bindings()` の confirmed entry を逆引き元にする。"""
    monkeypatch.setattr(jsonl_watcher, "_bindings", {})
    monkeypatch.setattr(jsonl_watcher, "_confirmed_paths", {})

    def register(pwa_sid: str, claude_sid: str, *, confirmed: bool = True) -> Path:
        jsonl_path = tmp_path / f"{claude_sid}.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        jsonl_watcher._bindings[pwa_sid] = jsonl_watcher._ClaudeBinding(
            tmux_sid=pwa_sid,
            claude_pid=0,
            claude_cwd=str(tmp_path),
            start_time=0.0,
            jsonl_path=jsonl_path,
            confirmed=confirmed,
        )
        return jsonl_path

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


def test_endpoint_stop_event_pushes_output(fake_agents, fake_bindings, captured_pushes):
    """Stop イベントは last_assistant_message を本文に push する。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_bindings("primary", "claude-internal-uuid")
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


def test_endpoint_notification_event_pushes_message(fake_agents, fake_bindings, captured_pushes):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_bindings("primary", "claude-internal-uuid")
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


def test_endpoint_unknown_event_acks_without_push(fake_agents, fake_bindings, captured_pushes):
    """未対応イベントは受信 ack のみ、 push しない。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_bindings("primary", "claude-internal-uuid")
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


def test_endpoint_non_pwa_session_is_ignored(fake_agents, fake_bindings, captured_pushes):
    """confirmed binding に存在しない claude_sid (= デスクトップ公式 / ターミナル直叩き)
    は ignored で push されないこと。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # fake_bindings で primary を登録しないので claude_sid は孤立
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


def test_endpoint_unconfirmed_binding_is_ignored(fake_agents, fake_bindings, captured_pushes):
    """confirmed=False の binding (= 確率窓マッチ由来) は逆引き対象に含めず push しない。
    regression: f5d0ca6 で list_bindings() に切替えた時、 list_bindings の serialize が
    confirmed を露出しておらず、 hooks_router の `info.get("confirmed")` が常に falsy で
    全 hook が non_pwa_session 扱いになる回帰を実機で起こした。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fake_bindings("primary", "claude-internal-uuid", confirmed=False)
    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    payload = {
        "hook_event_name": "Stop",
        "session_id": "claude-internal-uuid",
        "cwd": fake_agents["cwd"],
        "last_assistant_message": "should not push",
    }
    response = client.post("/hooks/event", json=payload)
    assert response.json() == {"ok": True, "ignored": "non_pwa_session"}
    assert captured_pushes == []


def test_any_event_with_pwa_sid_header_confirms_binding(
    fake_bindings, captured_pushes, monkeypatch, tmp_path
):
    """確定経路の回帰: SessionStart に限らず、 X-PWA-SID header + transcript_path を持つ
    任意イベント (= Stop 等) で binding が確定する。 これにより backend 再起動後も最初の
    hook 1 発で正しい jsonl に self-heal し、 確率窓マッチに頼らない (= 2026-05-29
    cross-contamination 対策の核)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # confirm_bind は _save_bindings で persist する副作用があるため tmp に逃がす。
    monkeypatch.setattr(jsonl_watcher, "_PERSIST_PATH", tmp_path / "bindings.json")
    transcript = tmp_path / "claude-xyz.jsonl"
    transcript.write_text("{}\n")

    app = FastAPI()
    app.include_router(hooks_router.router)
    client = TestClient(app)

    payload = {
        "hook_event_name": "Stop",
        "session_id": "claude-xyz",
        "transcript_path": str(transcript),
        "last_assistant_message": "done",
    }
    response = client.post("/hooks/event", json=payload, headers={"X-PWA-SID": "ses_tab"})
    assert response.status_code == 200
    # SessionStart でなく Stop でも、 header 経由で確定 binding が張られている。
    assert jsonl_watcher.get_jsonl_for("ses_tab") == transcript
