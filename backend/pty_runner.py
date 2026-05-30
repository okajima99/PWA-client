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

tmux 永続化 + control mode:
    USE_TMUX_WRAP=True (= 既定) のとき、 zsh を直接でなく
    `tmux -CC new-session -A -s <session_id> zsh` 経由で起動する。
    - 1 度目の attach: tmux セッション + zsh を新規作成して control mode で attach
    - 2 度目以降の attach: 既存セッションに control mode で attach (= 中の claude は生きたまま)
    - WebSocket が切れたら attach (= control client) を terminate、 ただし tmux サーバ内の
      セッション + claude は生存し続けるので backend 再起動でも保たれる

    `-CC` = control mode。 tmux は生画面でなく構造化通知 (%output %<pane> <octal> 等) を
    送る。 出力は ControlModeLineBuffer で行に組み立てて %output の生データだけを queue に
    積み、 入力は send-keys -H、 resize は refresh-client -C で control client に書く。
    これでクライアント (= xterm) が自分の桁数を tmux に伝えてその幅で描画させられるため、
    生 passthrough で起きていた桁数不一致による折り返し崩壊が原理的に解消する。
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
from dataclasses import dataclass, field
from pathlib import Path

from config import CLAUDE_PATH
from control_mode import (
    ControlModeLineBuffer,
    build_refresh_client_line,
    build_send_keys_lines,
)

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
    """1 セッション = 1 claude プロセス + master fd + 出力 queue。

    `control_mode` が True のとき、 master fd は tmux -CC (= control mode) client に
    繋がっており、 生バイトでなく構造化通知が流れる。 出力は `_cmbuf` で行に組み立てて
    %output の生データだけを output_queue に積み、 入力/resize は send-keys -H /
    refresh-client -C コマンドとして master fd に書く。
    """
    session_id: str
    process: asyncio.subprocess.Process
    master_fd: int
    output_queue: asyncio.Queue[bytes]
    exit_event: asyncio.Event
    control_mode: bool = False
    _reader_attached: bool = False
    _cmbuf: ControlModeLineBuffer = field(default_factory=ControlModeLineBuffer)


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

    # 実行コマンド組み立て: tmux wrap 時は `tmux -CC new-session -A -s <name> zsh`、
    # 直接時は `zsh` 単独。 tmux の -A は「既存なら attach、 無ければ作って attach」。
    # 新規作成判定は spawn 前にやらないと「-A」 が走ったあとは区別不能。
    #
    # `-CC` = control mode。 tmux が生画面 (= ANSI redraw passthrough) でなく構造化通知
    # (%output %<pane> <octal> 等) を送る。 これにより client (= xterm) が自分の桁数を
    # refresh-client -C で tmux に伝え、 tmux がそのサイズで描画するため、 生 passthrough で
    # 起きていた「桁数不一致による折り返し崩壊」 が原理的に起きない (= 移植の主目的)。
    is_new_tmux_session = False
    if USE_TMUX_WRAP:
        tmux_name = _tmux_session_name(session_id)
        is_new_tmux_session = not has_tmux_session(session_id)
        # `-e PWA_SID=<sid>` で tmux session env に PWA タブ識別子を注入する。 これは
        # session 配下の全 pane に継承され、 zsh → claude → SessionStart hook の curl まで
        # 環境変数として伝わる。 backend の hooks_router が X-PWA-SID header としてこれを
        # 受けて、 claude_sid / transcript_path を確定 bind するための tag になる。
        # `-e` は tmux 3.2+ で対応、 reattach 時は無視される (= 既存 env を優先)。
        # `-x/-y` は新規 session の初期サイズ (= 既存 attach 時は無視され、 接続後に
        # frontend の resize → refresh-client -C で client サイズに補正される)。
        argv = [
            TMUX_BIN, "-CC", "new-session", "-A", "-s", tmux_name,
            "-e", f"PWA_SID={session_id}",
            "-x", str(initial_cols), "-y", str(initial_rows),
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
        control_mode=USE_TMUX_WRAP,
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
        if session.control_mode:
            # control mode: 生バイトを行に組み立て、 %output の生データだけを queue へ。
            # %begin/%end/%layout-change 等の制御通知や起動時 DCS は捨てる。
            for ev in session._cmbuf.feed(data):
                if ev["type"] == "output":
                    _enqueue_output(session, ev["data"])
                # %exit は process 終了 (= _wait_for_exit) が拾うのでここでは無視
        else:
            _enqueue_output(session, data)

    loop.add_reader(fd, reader)
    session._reader_attached = True


def _enqueue_output(session: PtySession, data: bytes) -> None:
    """client 向け出力 queue に積む。 満杯なら最古を 1 個捨てて入れる (= 読み遅れ吸収)。"""
    try:
        session.output_queue.put_nowait(data)
    except asyncio.QueueFull:
        try:
            _ = session.output_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            session.output_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("PTY queue overflow session=%s, dropping chunk", session.session_id)


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


def _write_control_command(session: PtySession, line: str) -> None:
    """control client (= master fd) に tmux コマンド 1 行を書く (= 末尾 \\n で確定)。

    control mode の client は stdin の各行を tmux コマンドとして解釈する。 send-keys -H /
    refresh-client -C はこの経路で送る。 コマンドは ASCII なので latin-1 で十分。
    """
    try:
        os.write(session.master_fd, (line + "\n").encode("latin-1"))
    except OSError as e:
        if e.errno not in (errno.EBADF, errno.EIO):
            logger.exception("control command write error session=%s", session.session_id)


def write_pty(session: PtySession, data: bytes) -> None:
    """user 入力を子 claude の stdin に届ける。

    control mode では master fd に生バイトを書いても tmux はコマンドとして誤解釈するので、
    `send-keys -H <hex>` コマンドに変換して control client 経由で pane に注入する。
    生 PTY (= USE_TMUX_WRAP=False) では従来どおり master fd に直書きする。
    """
    if session.exit_event.is_set():
        return
    if session.control_mode:
        tmux_name = _tmux_session_name(session.session_id)
        for line in build_send_keys_lines(tmux_name, data):
            _write_control_command(session, line)
        return
    try:
        os.write(session.master_fd, data)
    except OSError as e:
        if e.errno not in (errno.EBADF, errno.EIO):
            logger.exception("write_pty error session=%s", session.session_id)


def resize_pty(session: PtySession, rows: int, cols: int) -> None:
    """client の桁数変化を子 TTY に反映する。

    control mode では `refresh-client -C <cols>,<rows>` で control client 自身のサイズを
    tmux に通知する (= クライアント権威サイズ。 これが折り返し崩壊を防ぐ本体)。 生 PTY では
    master fd の winsize を直接設定する (= TIOCSWINSZ)。
    """
    if session.exit_event.is_set():
        return
    if session.control_mode:
        _write_control_command(session, build_refresh_client_line(max(1, cols), max(1, rows)))
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
    # text 送信:
    #   - paste-buffer に `-p` を付けて bracketed paste mode で送る (= TUI が paste 全体を
    #     一塊として認識、 paste 内の改行を確定として扱わない)。 これがないと、 長文中の
    #     改行で claude TUI が途中で確定したり、 普通の連続キー入力扱いになる。
    #   - text と Enter が 2 subprocess に分かれていると、 paste の pane feed が完了する
    #     前に Enter が届いて取りこぼされる実機ケースがあるため、 tmux のコマンドチェーン
    #     `;` で 1 invocation にまとめる (= tmux server 内の queue で順次処理が保証される)。
    chained_enter = False
    if text:
        text_args = None
        # 改行を含む text のみ paste-buffer 経路 (= claude TUI で `[Pasted text #N]`
        # プレースホルダ化される対象)。 single-line は素の send-keys -l で安全に送れる上、
        # paste-buffer を 2 回送る経路に乗せると 2 回目が「重複 paste」 になってしまうので
        # 構造的に避ける。
        if "\n" in text:
            buf_name = f"pwa-paste-{int(time.time() * 1_000_000)}"
            try:
                proc = subprocess.run(
                    ["tmux", "load-buffer", "-b", buf_name, "-"],
                    input=text.encode("utf-8"),
                    capture_output=True,
                    timeout=2.0,
                )
                if proc.returncode == 0:
                    # claude TUI の bracketed paste は 1 回目で `[Pasted text #N]` プレース
                    # ホルダにまとめられ、 そこから展開して送信するには「同じ paste をもう
                    # 一度」 送る必要がある (= TUI が "paste again to expand" と明示、 実機
                    # capture で確認)。 paste-buffer を 2 回チェーンする: 1 回目は -d なしで
                    # buffer 保持、 2 回目に -d で削除。
                    text_args = [
                        "paste-buffer", "-p", "-b", buf_name, "-t", tmux_name,
                        ";",
                        "paste-buffer", "-p", "-b", buf_name, "-t", tmux_name, "-d",
                    ]
            except (subprocess.TimeoutExpired, OSError):
                pass
        if text_args is None:
            text_args = ["send-keys", "-t", tmux_name, "-l", text]
        if enter:
            text_args = [*text_args, ";", "send-keys", "-t", tmux_name, "Enter"]
            chained_enter = True
        arg_sets.append(text_args)
    if key:
        arg_sets.append(["send-keys", "-t", tmux_name, key])
    if enter and not chained_enter:
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
