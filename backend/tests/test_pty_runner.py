"""pty_runner の単体テスト。

claude を直接 spawn する関数はテスト中に走らせると I/O と subprocess 起動が要るので、
end-to-end は `/bin/cat` を代用 spawn して PTY ポンプ全体 (= spawn → write → read → terminate)
を検証する。 pure な防御コード (= env 検出 / 引数バリデーション / exit 後 no-op) は
直接 unit test。
"""
import asyncio
import os

import pytest

import pty_runner


@pytest.fixture
def restore_pty_sessions():
    """test 終了時に pty_runner.pty_sessions を綺麗にする。"""
    snap = dict(pty_runner.pty_sessions)
    yield
    # 残ったセッションは強制終了
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(pty_runner.shutdown_all())
    finally:
        loop.close()
    pty_runner.pty_sessions.clear()
    pty_runner.pty_sessions.update(snap)


@pytest.fixture
def restore_env():
    snap = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snap)


def test_spawn_rejects_anthropic_base_url(restore_env):
    """ANTHROPIC_BASE_URL が親 env に残ってたら起動拒否 (= penalty trigger 防御)。"""
    os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:8000/proxy"
    with pytest.raises(RuntimeError, match="ANTHROPIC_BASE_URL"):
        asyncio.run(pty_runner.spawn_pty_session("test-rejects-base-url"))


def test_spawn_rejects_empty_claude_path(monkeypatch, restore_env):
    """CLAUDE_PATH が空文字 / None だと spawn 拒否。"""
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    monkeypatch.setattr(pty_runner, "CLAUDE_PATH", "")
    with pytest.raises(RuntimeError, match="CLAUDE_PATH"):
        asyncio.run(pty_runner.spawn_pty_session("test-rejects-empty-path"))


def test_write_and_resize_after_exit_are_noops():
    """exit_event が立ってる session に write / resize しても OSError を投げない。"""
    # 最小限の fake session、 master_fd は閉じた状態
    import types

    fake_proc = types.SimpleNamespace(returncode=0)
    session = pty_runner.PtySession(
        session_id="closed",
        process=fake_proc,  # type: ignore[arg-type]
        master_fd=-1,
        output_queue=asyncio.Queue(),
        exit_event=asyncio.Event(),
    )
    session.exit_event.set()

    # どちらも例外ナシで早期 return すれば OK
    pty_runner.write_pty(session, b"ignored")
    pty_runner.resize_pty(session, 40, 120)


def test_capture_tmux_scrollback_disabled_when_no_wrap(monkeypatch):
    """USE_TMUX_WRAP=False では capture は常に b''。"""
    monkeypatch.setattr(pty_runner, "USE_TMUX_WRAP", False)
    assert pty_runner.capture_tmux_scrollback("anything") == b""


def test_has_tmux_session_disabled_when_no_wrap(monkeypatch):
    monkeypatch.setattr(pty_runner, "USE_TMUX_WRAP", False)
    assert pty_runner.has_tmux_session("anything") is False


def test_capture_tmux_scrollback_returns_empty_on_unknown(monkeypatch):
    """tmux に存在しない session を指したら returncode!=0 で空 bytes。"""
    monkeypatch.setattr(pty_runner, "USE_TMUX_WRAP", True)
    # 存在しないだろう name を渡す (= 仮に存在しても無害な空 capture)
    out = pty_runner.capture_tmux_scrollback("__nonexistent_test_session__")
    assert out == b""


def test_tmux_session_name_sanitizes_special_chars():
    """tmux に渡せない記号 (`.`, `:`, ` `) を `_` 化、 prefix で衝突避け。"""
    assert pty_runner._tmux_session_name("foo") == "pwa-foo"
    assert pty_runner._tmux_session_name("foo.bar:baz qux") == "pwa-foo_bar_baz_qux"
    assert pty_runner._tmux_session_name("alpha-1_2") == "pwa-alpha-1_2"


def test_write_pty_control_mode_emits_send_keys():
    """control mode の write_pty は master fd に send-keys -H コマンドを書く。"""
    import types

    r, w = os.pipe()
    try:
        session = pty_runner.PtySession(
            session_id="cm-write",
            process=types.SimpleNamespace(returncode=None),  # type: ignore[arg-type]
            master_fd=w,
            output_queue=asyncio.Queue(),
            exit_event=asyncio.Event(),
            control_mode=True,
        )
        pty_runner.write_pty(session, b"hi")
        written = os.read(r, 4096).decode("latin-1")
        # tmux_name は pwa-<sanitized>、 入力 "hi" = 68 69 の hex
        assert written == "send-keys -t pwa-cm-write -H 68 69\n"
    finally:
        os.close(r)
        os.close(w)


def test_resize_pty_control_mode_emits_refresh_client():
    """control mode の resize_pty は master fd に refresh-client -C を書く。"""
    import types

    r, w = os.pipe()
    try:
        session = pty_runner.PtySession(
            session_id="cm-resize",
            process=types.SimpleNamespace(returncode=None),  # type: ignore[arg-type]
            master_fd=w,
            output_queue=asyncio.Queue(),
            exit_event=asyncio.Event(),
            control_mode=True,
        )
        pty_runner.resize_pty(session, rows=30, cols=100)
        written = os.read(r, 4096).decode("latin-1")
        # cols,rows の順 (= refresh-client -C は <cols>,<rows>)
        assert written == "refresh-client -C 100,30\n"
    finally:
        os.close(r)
        os.close(w)


def test_spawn_cat_roundtrip(restore_env, restore_pty_sessions, monkeypatch):
    """`/bin/cat` を代用 spawn して PTY ポンプ全体を検証。

    cat は stdin を stdout にそのまま返すので、 write_pty → output_queue から
    同じバイト列が読めれば PTY pump が機能してる。 さらに terminate で
    exit_event が立つことも確認。

    USE_TMUX_WRAP=False に倒すのは、 test 終了後に tmux サーバ内にゴミセッションを
    残さないため (= test 環境を汚さない)。 tmux 込みの動作確認は別途
    integration test または smoketest で行う。
    """
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    # spawn は PTY_INITIAL_ARGV で起動するので、 ここを `/bin/cat` 1 個に差替えて
    # echo 子プロセスを試す。 CLAUDE_PATH は validation 用に残ってるので空でない値を入れる。
    monkeypatch.setattr(pty_runner, "PTY_INITIAL_ARGV", ["/bin/cat"])
    monkeypatch.setattr(pty_runner, "CLAUDE_PATH", "/bin/true")
    monkeypatch.setattr(pty_runner, "USE_TMUX_WRAP", False)

    async def scenario() -> None:
        session = await pty_runner.spawn_pty_session("roundtrip-test")
        assert session.process.returncode is None
        assert session.session_id == "roundtrip-test"

        marker = b"hello via PTY"
        pty_runner.write_pty(session, marker + b"\n")

        # PTY echo (= cat の出力) を最大 2 秒待つ。 PTY 経由なので OPOST が NL→CRLF に
        # 変換 + ICRNL が CR→NL に変換するため、 元の "\n" は受信側で "\r\n" として
        # 流れてくることがある。 substring 判定で吸収する。
        received = bytearray()
        deadline = asyncio.get_event_loop().time() + 2.0
        while marker not in bytes(received) and asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(session.output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            received.extend(chunk)
        assert marker in bytes(received), (
            f"expected {marker!r} in received={bytes(received)!r}"
        )

        await pty_runner.terminate_pty_session(session, timeout=2.0)
        assert session.exit_event.is_set()

    asyncio.run(scenario())
