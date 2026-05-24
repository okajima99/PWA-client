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

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# binding を JSON で persist する file。 backend 再起動跨ぎで confirmed binding を
# 保持して、 再起動直後の数秒窓 (= 各タブで /clear するまで確率窓マッチに頼る期間) を
# 潰す。 file が破損 / 読めなければ空起動。
_PERSIST_PATH = Path(__file__).resolve().parent.parent / "logs" / "jsonl_bindings.json"

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
    # SessionStart hook 経由で確定したか。 True なら startup scan / watcher event は
    # 一切上書きしない (= 確率窓マッチによる cross-contamination をブロック)。
    confirmed: bool = False


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
    # SessionStart hook で確定した binding は確率マッチ経路で絶対に上書きしない。
    if binding.confirmed:
        return False
    # 既に他の confirmed binding がこの JSONL を所有してたら、 確率マッチで別タブに
    # 横取りさせない (= cross-contamination 防止)。
    for other in _bindings.values():
        if other is binding:
            continue
        if other.confirmed and other.jsonl_path == path:
            return False
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
    existing = _bindings.get(tmux_sid)
    if existing is not None and existing.confirmed:
        # 既に SessionStart hook 経由で確定済。 claude_pid / start_time を最新値で更新だけ
        # 行い、 jsonl_path には触らない (= 確定値を保護)。
        existing.claude_pid = claude_pid
        existing.start_time = start_time
        return
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
    if _bindings.pop(tmux_sid, None) is not None:
        _save_bindings()


def confirm_bind(pwa_sid: str, claude_sid: str, transcript_path: str) -> Optional[Path]:
    """SessionStart hook 経由で呼ばれる確定 binding 経路。
    PWA タブで起動した claude のみが PWA_SID env を持っているので、 hooks_router が
    X-PWA-SID header からこの pwa_sid を取得して呼ぶ。 確率窓マッチを介さず
    claude_sid / transcript_path を直接受け取るので race / 誤割当ゼロ。
    /clear 等で新 claude_sid に切り替わっても hook が再発火して上書きされる。
    """
    path = Path(transcript_path)
    # SessionStart hook は claude 起動直後 (= JSONL birth 前) に発火するので、 ファイル
    # 存在 check はしない。 jsonl_path は文字列で保持しておけば後の get_jsonl_for で
    # is_file() check して合えば読み出す。
    binding = _bindings.get(pwa_sid)
    if binding is None:
        # PWA spawn 直後で _register_claude_when_ready がまだ動いてない / 失敗してた
        # ケース。 hook 経由の情報だけで binding を作る。 claude_pid は後で必要なら
        # 解決すれば良い (= 現状の registry 利用箇所では未使用)。
        binding = _ClaudeBinding(
            tmux_sid=pwa_sid, claude_pid=0,
            claude_cwd=str(path.parent),  # project dir のまま (= cwd 逆引きは不要)
            start_time=time.time(),
        )
        _bindings[pwa_sid] = binding
    binding.jsonl_path = path
    binding.confirmed = True
    # race 救済: on_created の確率マッチが先に走って別 binding (= confirmed でない古いタブ)
    # に同 JSONL が bind されてた場合、 ここで剥がして二重所有を解消する。 同じ JSONL を
    # 2 タブで tail すると同 chat が複数タブに流れる cross-contamination 再発。
    for other in _bindings.values():
        if other is binding:
            continue
        if other.jsonl_path == path:
            logger.info(
                "jsonl_watcher confirm_bind detached stale binding: pwa_sid=%s -> None",
                other.tmux_sid,
            )
            other.jsonl_path = None
    logger.info(
        "jsonl_watcher confirm_bound pwa_sid=%s claude_sid=%s -> %s",
        pwa_sid, claude_sid, path.name,
    )
    _save_bindings()
    return path


def force_bind(tmux_sid: str, jsonl_filename: str, cwd: str) -> Optional[Path]:
    """応急処置用: 指定 PWA session に JSONL を強制紐付けする。
    確率窓マッチ (= startup scan の ±_BIRTH_WINDOW 失敗) で誤割当 / 未割当が起きた時に、
    debug endpoint から手動で binding を矯正するための逃げ道。
    """
    path = _cwd_to_project_dir(cwd) / jsonl_filename
    if not path.is_file():
        logger.warning("force_bind: file not found %s", path)
        return None
    binding = _bindings.get(tmux_sid)
    if binding is None:
        binding = _ClaudeBinding(
            tmux_sid=tmux_sid, claude_pid=0, claude_cwd=cwd, start_time=time.time(),
        )
        _bindings[tmux_sid] = binding
    binding.jsonl_path = path
    logger.info("jsonl_watcher force_bound sid=%s -> %s", tmux_sid, path.name)
    return path


def list_bindings() -> dict[str, dict]:
    """debug 用: 現在の全 binding を JSON-serializable な dict で返す。"""
    return {
        sid: {
            "claude_pid": b.claude_pid,
            "claude_cwd": b.claude_cwd,
            "start_time": b.start_time,
            "jsonl_path": str(b.jsonl_path) if b.jsonl_path else None,
        }
        for sid, b in _bindings.items()
    }


def get_jsonl_for(tmux_sid: str) -> Optional[Path]:
    """確定済の JSONL path を返す。 未確定 / ファイル消失なら None。"""
    binding = _bindings.get(tmux_sid)
    if binding is None or binding.jsonl_path is None:
        return None
    if not binding.jsonl_path.is_file():
        return None
    return binding.jsonl_path


def _save_bindings() -> None:
    """confirmed binding だけ JSON で persist する。
    register_pending / on_created 経由の確率窓マッチは backend lifetime のみ有効 (= 再起動
    後の意味が薄い + ノイズになる) なので保存しない。 SessionStart hook 由来の確定値だけ
    残せば、 再起動後も該当 PWA タブが /clear せず chat 継続できる。
    atomic write (= tmp + os.replace) で書き込み中の crash でも壊れない。
    """
    try:
        _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sid: {
                "tmux_sid": b.tmux_sid,
                "claude_pid": b.claude_pid,
                "claude_cwd": b.claude_cwd,
                "start_time": b.start_time,
                "jsonl_path": str(b.jsonl_path) if b.jsonl_path else None,
                "confirmed": b.confirmed,
            }
            for sid, b in _bindings.items() if b.confirmed
        }
        tmp = _PERSIST_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PERSIST_PATH)
    except OSError:
        logger.exception("jsonl_watcher _save_bindings failed")


def _load_bindings() -> None:
    """backend 起動時に persist file から confirmed binding を復元する。
    file が無い / 壊れてる / JSONL 実体が消えてる binding は無視。 復元後の binding は
    confirmed=True で乗るので、 確率窓マッチでの上書きから保護される。 該当 tmux session
    が既に死んでた場合、 binding は残るが get_jsonl_for で is_file() check が通れば
    chat tail は機能する (= claude プロセスが死んでても過去 JSONL は読める)。
    """
    if not _PERSIST_PATH.is_file():
        return
    try:
        data = json.loads(_PERSIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("jsonl_watcher _load_bindings: file corrupted, ignoring")
        return
    restored = 0
    for sid, d in data.items():
        jp = d.get("jsonl_path")
        if not jp:
            continue
        path = Path(jp)
        if not path.is_file():
            continue
        _bindings[sid] = _ClaudeBinding(
            tmux_sid=d.get("tmux_sid", sid),
            claude_pid=int(d.get("claude_pid") or 0),
            claude_cwd=d.get("claude_cwd", str(path.parent)),
            start_time=float(d.get("start_time") or time.time()),
            jsonl_path=path,
            confirmed=bool(d.get("confirmed", True)),
        )
        restored += 1
    if restored:
        logger.info("jsonl_watcher _load_bindings: restored %d binding(s)", restored)


def start_watcher() -> None:
    global _observer
    if _observer is not None:
        return
    _CLAUDE_PROJECTS.mkdir(parents=True, exist_ok=True)
    _load_bindings()
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
