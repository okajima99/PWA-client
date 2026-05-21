"""PTY-attached claude CLI runner.

`claude` を実 pseudo-terminal で起動し、 terminal 直叩きと区別不能な経路にする。
これにより Anthropic の 3rd-party programmatic penalty を回避する (= docs/pty-migration.md §1.3)。

絶対制約 (= penalty 回避の必要条件、 全て守る):
    - claude-agent-sdk を import / 経由しない
    - ANTHROPIC_BASE_URL を子 env に渡さない (= 親 env にも設定されてないことを起動時確認)
    - --print / --output-format / --input-format / --permission-mode 等 programmatic 印を渡さない
    - slave fd を子の stdin/stdout/stderr に dup して、 子で isatty() True
    - bypassPermissions 系の flag を渡さない (= default interactive)

非同期設計:
    - pty.openpty() で master/slave fd ペア生成
    - asyncio.create_subprocess_exec で claude を spawn、 slave を子 fd に
    - loop.add_reader で master fd を非ブロッキング read、 chunk を Queue に積む
    - WebSocket route 側が Queue を await して client に流し、 client 入力は write_pty で master に書く
    - resize は TIOCSWINSZ ioctl で master fd に通知
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pty
import struct
import termios
from dataclasses import dataclass

from config import CLAUDE_PATH

logger = logging.getLogger(__name__)

# 同時稼働 PTY セッション (= session_id -> PtySession)。
# module-level に置くことで state.py への import 循環を避けつつ shutdown から到達可能。
pty_sessions: dict[str, "PtySession"] = {}


@dataclass
class PtySession:
    """1 セッション = 1 claude プロセス + master fd + 出力 queue。"""
    session_id: str
    process: asyncio.subprocess.Process
    master_fd: int
    output_queue: asyncio.Queue[bytes]
    exit_event: asyncio.Event
    _reader_attached: bool = False


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """master fd 経由で子の TTY 行/列を設定 (= TIOCSWINSZ)。"""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _make_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


async def spawn_pty_session(
    session_id: str,
    cwd: str | None = None,
    initial_rows: int = 40,
    initial_cols: int = 120,
) -> PtySession:
    """claude を PTY 経由で起動して PtySession を返す。

    起動時 sanity check: ANTHROPIC_BASE_URL が親 env に残ってたら起動を拒否。
    残ってると子 claude が proxy 経由になって penalty trigger を踏む。
    """
    if os.environ.get("ANTHROPIC_BASE_URL"):
        raise RuntimeError(
            "ANTHROPIC_BASE_URL is set in backend env; "
            "PTY runner must not route claude through any proxy. "
            "Unset and restart backend."
        )
    if not CLAUDE_PATH:
        raise RuntimeError("CLAUDE_PATH is empty; set `claude_path` in backend/config.json")

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, initial_rows, initial_cols)

    # 子 env: 親をそのまま継承、 ただし penalty trigger になりうる変数は明示的に剥がす
    child_env = dict(os.environ)
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"):
        child_env.pop(var, None)
    # TTY 想定の TERM を確保 (= 親 server が daemon 起動だと TERM 無いことがある)
    child_env.setdefault("TERM", "xterm-256color")

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_PATH,
            # programmatic 印になる引数は一切渡さない
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=child_env,
            # 親 backend の controlling tty を継承させない: 新 session leader にする
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # 子に dup されたので親側 slave_fd は不要、 leak すると tty 解放されない
        os.close(slave_fd)

    session = PtySession(
        session_id=session_id,
        process=proc,
        master_fd=master_fd,
        output_queue=asyncio.Queue(maxsize=1024),
        exit_event=asyncio.Event(),
    )
    _attach_reader(session)
    asyncio.create_task(_wait_for_exit(session))
    logger.info("spawned PTY session=%s pid=%s cwd=%s", session_id, proc.pid, cwd)
    return session


def _attach_reader(session: PtySession) -> None:
    """master fd を非ブロッキングにして loop.add_reader でドレインする。"""
    loop = asyncio.get_event_loop()
    fd = session.master_fd
    _make_nonblocking(fd)

    def reader() -> None:
        try:
            data = os.read(fd, 4096)
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return
            if e.errno == errno.EIO:
                # 子が PTY を閉じた (= 通常終了 or kill)
                logger.debug("PTY EIO session=%s, detaching reader", session.session_id)
                loop.remove_reader(fd)
                return
            logger.exception("PTY read error session=%s", session.session_id)
            loop.remove_reader(fd)
            return
        if not data:
            return
        try:
            session.output_queue.put_nowait(data)
        except asyncio.QueueFull:
            # client が読み遅れてる、 古いものを 1 個捨てて新規を入れる
            try:
                _ = session.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                session.output_queue.put_nowait(data)
            except asyncio.QueueFull:
                logger.warning("PTY queue overflow session=%s, dropping chunk", session.session_id)

    loop.add_reader(fd, reader)
    session._reader_attached = True


async def _wait_for_exit(session: PtySession) -> None:
    try:
        await session.process.wait()
    finally:
        session.exit_event.set()
        loop = asyncio.get_event_loop()
        try:
            loop.remove_reader(session.master_fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        logger.info(
            "PTY session=%s exited rc=%s",
            session.session_id,
            session.process.returncode,
        )


def write_pty(session: PtySession, data: bytes) -> None:
    """user 入力を子 claude の stdin (= PTY master) に書く。"""
    if session.exit_event.is_set():
        return
    try:
        os.write(session.master_fd, data)
    except OSError as e:
        if e.errno not in (errno.EBADF, errno.EIO):
            logger.exception("write_pty error session=%s", session.session_id)


def resize_pty(session: PtySession, rows: int, cols: int) -> None:
    if session.exit_event.is_set():
        return
    try:
        _set_winsize(session.master_fd, max(1, rows), max(1, cols))
    except OSError:
        logger.exception("resize_pty error session=%s", session.session_id)


async def terminate_pty_session(session: PtySession, timeout: float = 3.0) -> None:
    """SIGTERM → timeout で SIGKILL の段階終了。"""
    if session.process.returncode is not None:
        session.exit_event.set()
        return
    try:
        session.process.terminate()
        await asyncio.wait_for(session.process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("PTY session=%s did not terminate, killing", session.session_id)
        session.process.kill()
        await session.process.wait()
    finally:
        session.exit_event.set()


async def shutdown_all() -> None:
    """backend shutdown 時、 全 PTY セッションを綺麗に閉じる。"""
    for session_id, session in list(pty_sessions.items()):
        try:
            await terminate_pty_session(session)
        except Exception:
            logger.exception("shutdown_all: failed to terminate session=%s", session_id)
    pty_sessions.clear()
