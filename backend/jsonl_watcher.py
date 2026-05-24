"""tmux session ↔ claude プロセス ↔ JSONL の紐付けを backend mem で確定保持する。

仕組み:
    1. tmux + claude を spawn したあと、 backend が claude プロセスの PID と起動時刻を
       `register_pending` で登録する
    2. watchdog で `~/.claude/projects/` 配下を再帰監視
    3. 新規 .jsonl ファイル が作成されたら、 同 cwd の pending binding のうち
       「claude 起動時刻と JSONL birthtime の差が _BIRTH_WINDOW 秒以内」 なものを
       1 個に絞って紐付ける
    4. `/clear` 等で同 claude プロセス (= 同 pid) が新 JSONL を作った場合も同じ経路で
       jsonl_path を上書き

statusline / tmux-session-map には依存しない。 同 cwd で並行する他 claude プロセス
(Claude Desktop App / ターミナル直叩き等) は別 PID / 別タイミングで birth するので、
窓 ± _BIRTH_WINDOW 秒以上ずれていれば構造的に区別できる。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# claude 起動時刻と JSONL birthtime のマッチ許容窓 (秒)。 これより狭いと
# launch_alias 経由の起動遅延 (1-2 秒) を吸収できない、 これより広いと並行 claude
# プロセスとの識別精度が落ちる。
_BIRTH_WINDOW = 10.0


@dataclass
class _ClaudeBinding:
    tmux_sid: str
    claude_pid: int
    claude_cwd: str
    start_time: float
    jsonl_path: Optional[Path] = None


# tmux_sid → binding
_bindings: dict[str, _ClaudeBinding] = {}
_observer: Optional[Observer] = None


def _cwd_to_project_dirname(cwd: str) -> str:
    """claude Code の規約: パス中の `/` と `.` を `-` に置換 (先頭 `/` も `-`)。"""
    return cwd.replace("/", "-").replace(".", "-")


def _cwd_to_project_dir(cwd: str) -> Path:
    return _CLAUDE_PROJECTS / _cwd_to_project_dirname(cwd)


def _file_birthtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_birthtime
    except (OSError, AttributeError):
        return None


def _try_bind_jsonl(binding: _ClaudeBinding, path: Path, birth: float) -> bool:
    """JSONL を binding に紐付ける判定。

    紐付け条件:
      - 同じ cwd の project dir 配下
      - claude 起動時刻 ± _BIRTH_WINDOW 以内に birth した
    既に jsonl_path が設定済の場合は、 上書き条件:
      - 新 path != 現 path
      - 新 birth が現 jsonl_path より新しい (= /clear で新 JSONL 作成)
    """
    expect_dir = _cwd_to_project_dir(binding.claude_cwd)
    if path.parent != expect_dir:
        return False
    if abs(birth - binding.start_time) > _BIRTH_WINDOW:
        # 既存 binding の /clear ケースは start_time から遠くても許容する: 同 binding が
        # 既に 1 度紐付け済で、 新 JSONL が現 jsonl_path より新しい mtime を持つ場合のみ
        if binding.jsonl_path is None:
            return False
        if path == binding.jsonl_path:
            return False
        try:
            new_mt = path.stat().st_mtime
            old_mt = binding.jsonl_path.stat().st_mtime
        except OSError:
            return False
        if new_mt <= old_mt:
            return False
    binding.jsonl_path = path
    return True


class _JsonlHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".jsonl":
            return
        birth = _file_birthtime(path)
        if birth is None:
            return
        for binding in list(_bindings.values()):
            if _try_bind_jsonl(binding, path, birth):
                logger.info(
                    "jsonl_watcher bound sid=%s pid=%s -> %s",
                    binding.tmux_sid, binding.claude_pid, path.name,
                )


def register_pending(
    tmux_sid: str, claude_pid: int, claude_cwd: str, start_time: float,
) -> None:
    """spawn 直後に呼ぶ。 birth event を待つ pending binding を登録する。

    登録の直後、 同 cwd の **既存** JSONL を 1 回スキャンして、 起動時刻に近い birthtime
    のものがあれば即紐付ける (= backend 再起動跨ぎでの recover、 watchdog event を
    既に取り逃がしたケースの救済)。
    """
    binding = _ClaudeBinding(
        tmux_sid=tmux_sid,
        claude_pid=claude_pid,
        claude_cwd=claude_cwd,
        start_time=start_time,
    )
    _bindings[tmux_sid] = binding

    proj = _cwd_to_project_dir(claude_cwd)
    if proj.is_dir():
        best: Optional[tuple[Path, float]] = None
        for j in proj.glob("*.jsonl"):
            bt = _file_birthtime(j)
            if bt is None:
                continue
            if abs(bt - start_time) > _BIRTH_WINDOW:
                continue
            if best is None or abs(bt - start_time) < abs(best[1] - start_time):
                best = (j, bt)
        if best is not None:
            binding.jsonl_path = best[0]
            logger.info(
                "jsonl_watcher startup-bound sid=%s pid=%s -> %s",
                tmux_sid, claude_pid, best[0].name,
            )


def unregister(tmux_sid: str) -> None:
    _bindings.pop(tmux_sid, None)


def get_jsonl_for(tmux_sid: str) -> Optional[Path]:
    """確定済の JSONL path を返す。 未確定 / ファイル消失なら None。"""
    binding = _bindings.get(tmux_sid)
    if binding is None or binding.jsonl_path is None:
        return None
    if not binding.jsonl_path.is_file():
        return None
    return binding.jsonl_path


def start_watcher() -> None:
    global _observer
    if _observer is not None:
        return
    _CLAUDE_PROJECTS.mkdir(parents=True, exist_ok=True)
    _observer = Observer()
    _observer.schedule(_JsonlHandler(), str(_CLAUDE_PROJECTS), recursive=True)
    _observer.start()
    logger.info("jsonl_watcher started watching %s", _CLAUDE_PROJECTS)


def stop_watcher() -> None:
    global _observer
    if _observer is None:
        return
    _observer.stop()
    _observer.join(timeout=2)
    _observer = None
    logger.info("jsonl_watcher stopped")
