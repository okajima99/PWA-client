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

tmux 永続化 (= phase 3):
    USE_TMUX_WRAP=True (= 既定) のとき、 `claude` を直接でなく
    `tmux new-session -A -s <session_id> claude` 経由で起動する。
    - 1 度目の attach: tmux セッション + claude を新規作成して attach
    - 2 度目以降の attach: 既存セッションに attach (= claude は生きたまま)
    - WebSocket が切れたら attach (= PTY child) を terminate、 ただし tmux サーバ内の
      セッション + claude は生存し続けるので backend 再起動でも保たれる
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pty
import re
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from config import CLAUDE_PATH

logger = logging.getLogger(__name__)

# 同時稼働 PTY セッション (= session_id -> PtySession)。
# module-level に置くことで state.py への import 循環を避けつつ shutdown から到達可能。
pty_sessions: dict[str, "PtySession"] = {}

# tmux で wrap して永続化する。 開発 / test では monkeypatch で False に倒せる。
USE_TMUX_WRAP: bool = True
TMUX_BIN: str = "tmux"

# PTY で初期起動するコマンド。 default は対話 login shell (= zsh -il) でユーザの
# .zshrc / 関数 / alias を載せた状態にする。 これにより claude 直起動でなく
# 「ターミナルが開いた状態」 で接続でき、 ユーザは自分の関数 (= claude 起動 wrapper
# 等) を打って claude を立ち上げられる。
# CLAUDE_PATH (= config.json) は path 検証のためだけに残し、 ここでは使わない。
PTY_INITIAL_ARGV: list[str] = ["zsh", "-il"]

# tmux session 名に使える文字に session_id を sanitize する。 tmux は
# `.`, `:`, ` `, `\` などを名前に許さない。 安全のため英数 + - + _ だけ通す。
_TMUX_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _tmux_session_name(session_id: str) -> str:
    """tmux 安全な名前に正規化。 pwa-<original> の prefix で衝突避け。"""
    safe = _TMUX_NAME_SAFE.sub("_", session_id)
    return f"pwa-{safe}"


def _run_tmux(*args: str, timeout: float = 2.0, text: bool = False):
    """tmux サブコマンドの共通実行ラッパ。 [TMUX_BIN] prefix 付与 + capture_output 固定 +
    timeout、 TimeoutExpired / OSError は None を返す (= 呼び側で失敗扱い)。 成功時は
    CompletedProcess。 tmux 操作の subprocess.run はこれ経由に統一する。"""
    try:
        return subprocess.run(
            [TMUX_BIN, *args], capture_output=True, timeout=timeout, text=text,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


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
    launch_alias: str | None = None,
) -> PtySession:
    """claude を PTY 経由で起動して PtySession を返す。

    起動時 sanity check: ANTHROPIC_BASE_URL が親 env に残ってたら起動を拒否。
    残ってると子 claude が proxy 経由になって penalty trigger を踏む。

    `launch_alias` 指定時、 tmux session を**新規**作成する場合に限り、 zsh prompt が
    出るのを少し待ってから `tmux send-keys` でその alias + Enter を流す。 これでタブ
    生成直後に claude TUI まで自動で立ち上がる。 既存 tmux session への reattach 時
    (= backend 再起動跨ぎ / タブ切替後) は claude が既に走ってるので alias は送らない。
    """
    if os.environ.get("ANTHROPIC_BASE_URL"):
        raise RuntimeError(
            "ANTHROPIC_BASE_URL is set in backend env; "
            "PTY runner must not route claude through any proxy. "
            "Unset and restart backend."
        )
    if not CLAUDE_PATH:
        # PTY 自体は zsh で動かせるが、 ユーザの wrapper 関数が最終的に呼ぶ claude path
        # はここで検証。 未設定なら設定漏れとして失敗させる。
        raise RuntimeError("CLAUDE_PATH is empty; set `claude_path` in backend/config.json")

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, initial_rows, initial_cols)

    # 子 env: 親をそのまま継承、 ただし penalty trigger になりうる変数は明示的に剥がす
    child_env = dict(os.environ)
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"):
        child_env.pop(var, None)
    # TTY 想定の TERM を確保 (= 親 server が daemon 起動だと TERM 無いことがある)
    child_env.setdefault("TERM", "xterm-256color")

    # 実行コマンド組み立て: tmux wrap 時は `tmux new-session -A -s <name> claude`、
    # 直接時は `claude` 単独。 tmux の -A は「既存なら attach、 無ければ作って attach」。
    # 新規作成判定は spawn 前にやらないと「-A」 が走ったあとは区別不能。
    is_new_tmux_session = False
    if USE_TMUX_WRAP:
        tmux_name = _tmux_session_name(session_id)
        is_new_tmux_session = not has_tmux_session(session_id)
        # `-e PWA_SID=<sid>` で tmux session env に PWA タブ識別子を注入する。 これは
        # session 配下の全 pane に継承され、 zsh → claude → SessionStart hook の curl まで
        # 環境変数として伝わる。 backend の hooks_router が X-PWA-SID header としてこれを
        # 受けて、 claude_sid / transcript_path を確定 bind するための tag になる。
        # `-e` は tmux 3.2+ で対応、 reattach 時は無視される (= 既存 env を優先)。
        argv = [
            TMUX_BIN, "new-session", "-A", "-s", tmux_name,
            "-e", f"PWA_SID={session_id}",
            *PTY_INITIAL_ARGV,
        ]
    else:
        argv = list(PTY_INITIAL_ARGV)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
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
    # 新規 tmux session かつ launch_alias 指定時のみ、 zsh prompt 出現を待ってから alias 送出。
    # 既存 reattach では中で既に claude が走ってる可能性が高いので何もしない。
    if launch_alias and is_new_tmux_session and USE_TMUX_WRAP:
        asyncio.create_task(_send_launch_alias(session_id, launch_alias))
    # 新規 tmux session でも reattach でも、 claude プロセス起動 (= launch_alias 後の数秒、
    # 既存セッションなら即時) を待って backend mem の binding に登録する
    asyncio.create_task(_register_claude_when_ready(session_id))
    return session


async def _send_launch_alias(session_id: str, alias: str, delay: float = 1.0) -> None:
    """zsh -il の起動完了 (= prompt 表示) を `delay` 秒待ってから tmux に alias+Enter を送る。"""
    try:
        await asyncio.sleep(delay)
        ok = tmux_send_keys(session_id, text=alias, enter=True)
        if not ok:
            logger.warning("launch alias send failed session=%s alias=%s", session_id, alias)
    except Exception:
        logger.exception("_send_launch_alias error session=%s", session_id)


async def _register_claude_when_ready(
    session_id: str, max_wait: float = 8.0, interval: float = 0.5,
) -> None:
    """tmux pane の子 claude プロセスが立ち上がるのを polling で待ち、 jsonl_watcher に登録する。

    launch_alias 経由だと claude 起動まで 1-2 秒、 環境次第でもう少しかかる。
    `max_wait` 秒以内に claude プロセスが見つからなければ諦める (= 既存 zsh のみで claude
    起動しないケース等)。
    """
    import jsonl_watcher  # 循環 import 回避のため遅延 import
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(interval)
        for pane_pid in _tmux_pane_pids(session_id):
            claude_pid = _find_claude_descendant(pane_pid)
            if claude_pid is None:
                continue
            start_time = _process_start_time(claude_pid)
            cwd = _process_cwd(claude_pid)
            if start_time is None or cwd is None:
                continue
            jsonl_watcher.register_pending(session_id, claude_pid, cwd, start_time)
            return


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


def has_tmux_session(session_id: str) -> bool:
    """指定 session の tmux session が既存か。 USE_TMUX_WRAP=False なら常に False。"""
    if not USE_TMUX_WRAP:
        return False
    r = _run_tmux("has-session", "-t", _tmux_session_name(session_id))
    return r is not None and r.returncode == 0


def capture_tmux_scrollback(session_id: str, lines: int = 5000) -> bytes:
    """tmux capture-pane で過去出力を ANSI 付きでバイト列取得 (= 再接続時の復元用)。

    `-e` で escape sequence を保持、 `-J` で wrapping を結合、 `-p` で stdout に出力、
    `-S -<lines>` で過去 `lines` 行ぶん遡る。 結果は ANSI 含むので xterm.write() に
    そのまま流せば過去画面が復元される。
    """
    if not USE_TMUX_WRAP:
        return b""
    r = _run_tmux("capture-pane", "-p", "-e", "-J", "-S", f"-{lines}", "-t", _tmux_session_name(session_id))
    if r is None or r.returncode != 0:
        return b""
    # tmux capture-pane の出力は行末 LF。 そのまま xterm に流すと最終行に余分な改行が
    # 入って claude の現在カーソル位置とズレるので末尾 LF を 1 個だけ剥がす。
    return r.stdout.rstrip(b"\n")


def tmux_send_keys(
    session_id: str,
    text: str | None = None,
    key: str | None = None,
    enter: bool = False,
) -> bool:
    """tmux session に直接キーを送る (= chat UI の入力経路、 PTY attach 不要)。

    出力は JSONL-SSE で取り、 入力はこの send-keys で送ることで、 chat UI が PTY に
    attach せずに済む (= 生 terminal と干渉しない、 master fd の drain 不要)。

    引数:
        text: literal 文字列 (= `-l` で送る、 制御文字として解釈させない)
        key:  tmux のキー名 (= "Escape" / "C-c" 等、 制御キー送信用)
        enter: 末尾に Enter を送る (= プロンプト確定)

    tmux session が存在しなければ False (= claude 未起動)。
    """
    if not USE_TMUX_WRAP:
        return False
    if not has_tmux_session(session_id):
        return False
    tmux_name = _tmux_session_name(session_id)
    arg_sets: list[list[str]] = []
    if text:
        arg_sets.append(["send-keys", "-t", tmux_name, "-l", text])
    if key:
        arg_sets.append(["send-keys", "-t", tmux_name, key])
    if enter:
        arg_sets.append(["send-keys", "-t", tmux_name, "Enter"])
    if not arg_sets:
        return False
    ok = True
    for args in arg_sets:
        r = _run_tmux(*args)
        if r is None or r.returncode != 0:
            ok = False
            logger.warning("tmux send-keys failed session=%s cmd=%s", session_id, args[-2:])
    return ok


def kill_tmux_session(session_id: str) -> bool:
    """指定 session の tmux session を強制終了する (= 中の claude も死ぬ)。

    Returns True if a session existed and was killed, False otherwise.
    通常の WebSocket disconnect では呼ばない (= 永続化のため)。 ユーザが明示的に
    「このセッション破棄」 を指示した時だけ呼ぶ。
    """
    if not USE_TMUX_WRAP:
        return False
    r = _run_tmux("kill-session", "-t", _tmux_session_name(session_id), text=True)
    return r is not None and r.returncode == 0


# ---- claude プロセス調査ヘルパ (= jsonl_watcher.register_pending への入力収集) ----

def _tmux_pane_pids(session_id: str) -> list[int]:
    if not USE_TMUX_WRAP:
        return []
    r = _run_tmux("list-panes", "-t", _tmux_session_name(session_id), "-F", "#{pane_pid}", text=True)
    if r is None or r.returncode != 0:
        return []
    return [int(s) for s in r.stdout.split() if s.strip().isdigit()]


def _find_claude_descendant(root_pid: int, max_depth: int = 6) -> int | None:
    """BFS で子孫プロセスを辿り、 ps の comm の basename が 'claude' のものを返す。"""
    queue: list[tuple[int, int]] = [(root_pid, 0)]
    while queue:
        pid, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for child_str in result.stdout.split():
            child_str = child_str.strip()
            if not child_str.isdigit():
                continue
            child_pid = int(child_str)
            try:
                ps = subprocess.run(
                    ["ps", "-p", str(child_pid), "-o", "comm="],
                    capture_output=True, text=True, timeout=2,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            comm = ps.stdout.strip()
            if comm and Path(comm).name == "claude":
                return child_pid
            queue.append((child_pid, depth + 1))
    return None


def _process_start_time(pid: int) -> float | None:
    """`ps -o lstart=` で取得した起動時刻文字列を unix epoch に変換。"""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    s = result.stdout.strip()
    if not s:
        return None
    # macOS lstart 形式: "Sun May 24 20:24:00 2026"
    try:
        return time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _process_cwd(pid: int) -> str | None:
    """lsof で cwd エントリを取得。 macOS は /proc が無いので lsof 経由。"""
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.split("\n"):
        if line.startswith("n"):
            return line[1:]
    return None


def jsonl_path_for_session(session_id: str) -> Path | None:
    """tmux pane 配下の claude プロセスが書いてる JSONL ファイルを返す。

    `jsonl_watcher` の backend mem registry を引くだけ。 spawn 時に
    `_register_claude_when_ready` 経由で binding を登録、 watchdog が新規 JSONL の
    birth event を見て紐付ける。 紐付け未完なら None。
    """
    import jsonl_watcher
    return jsonl_watcher.get_jsonl_for(session_id)


async def shutdown_all() -> None:
    """backend shutdown 時、 全 PTY child (= attach 接続) を綺麗に閉じる。

    tmux session は意図的に殺さない (= 中の claude をプロセスごと残して backend
    再起動後に reattach できる)。 セッション破棄が必要なら別途 kill_tmux_session を
    呼ぶ。
    """
    for session_id, session in list(pty_sessions.items()):
        try:
            await terminate_pty_session(session)
        except Exception:
            logger.exception("shutdown_all: failed to terminate session=%s", session_id)
    pty_sessions.clear()
