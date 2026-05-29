"""サスティナビリティ維持タスク群: stale tmux session kill / 古い JSONL 削除 /
statusline map cleanup。 backend lifespan task の起動時 + 定期実行で呼ばれる。

恒久的に増えていくリソース (= 放置すると無限蓄積する) を機械的に整理する箇所。
backend logs (RotatingFileHandler) / uploads/tmp (1h GC) は別経路で既対策済み、
このモジュールは以下を担当:

  1. PWA タブ削除後の残骸 tmux session の kill
  2. 古い JSONL ファイル (~/.claude/projects/) の自動削除 (mtime + quota)
  3. 古い statusline map ファイルの削除 (対応 tmux session 無いもの)
  4. 肥大化した Sunshine プロセスの restart (= 画面共有 encoder のメモリリーク対策)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from config import TMUX_SESSION_MAP_DIR

logger = logging.getLogger(__name__)


# 保持基準: mtime が KEEP_DAYS 日以内 = 残す、 それより古いものは削除候補。
# 加えて、 残った合計が MAX_BYTES を超える場合は古い方から削除して quota 内に収める。
JSONL_KEEP_DAYS = 30
JSONL_MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
# 定期実行間隔。 起動時に 1 回 + 24 時間ごと。
MAINTENANCE_INTERVAL_SEC = 24 * 3600

# Sunshine が画面共有 encoder のメモリをこの phys_footprint 超で抱えていたら restart
# 対象とみなす。 観測実績では idle 放置 + ゾンビストリームで 30 GB まで膨らんだ (= 大半は
# swap 退避され RSS には出ない、 phys_footprint でしか捕捉できない)。 fresh 起動直後は
# 数十 MB なので、 2 GB はリークを確実に捉えつつ正常稼働を巻き込まない閾値。
SUNSHINE_FOOTPRINT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def cleanup_stale_tmux_sessions() -> int:
    """sessions_meta に登録されていない pwa-ses_* tmux session を kill する。
    PWA タブを UI から削除した時点で sessions_meta から消えるが、 backend 経路を経ずに
    削除されたケース (= 旧バックエンドの残骸 / 手動削除) で tmux session だけ残るのを掃除。"""
    try:
        from state import sessions_meta
    except ImportError:
        return 0
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if r.returncode != 0:
        return 0
    killed = 0
    for name in r.stdout.splitlines():
        if not name.startswith("pwa-"):
            continue
        sid = name[4:]  # "pwa-ses_xxx" → "ses_xxx"
        if sid in sessions_meta:
            continue
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True, timeout=2.0,
            )
            logger.info("maintenance: killed stale tmux session %s", name)
            killed += 1
        except (subprocess.TimeoutExpired, OSError):
            pass
    return killed


def cleanup_stale_statusline_map() -> int:
    """設定で指定された statusline の tmux-session-map ディレクトリから、 対応する
    tmux session が既に存在しない pwa-* エントリを削除する。 statusline スクリプトが
    書く map ファイルが tmux session 終了後も残り続けるので、 起動時に整理する。"""
    if not TMUX_SESSION_MAP_DIR:
        return 0
    map_dir = Path(TMUX_SESSION_MAP_DIR).expanduser()
    if not map_dir.is_dir():
        return 0
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    existing = set(r.stdout.splitlines()) if r.returncode == 0 else set()
    removed = 0
    for f in map_dir.iterdir():
        if not f.is_file() or not f.name.startswith("pwa-"):
            continue
        if f.name in existing:
            continue
        try:
            f.unlink()
            logger.info("maintenance: removed stale statusline map %s", f.name)
            removed += 1
        except OSError:
            pass
    return removed


def cleanup_old_jsonl(
    keep_days: int = JSONL_KEEP_DAYS,
    max_bytes: int = JSONL_MAX_BYTES,
) -> int:
    """~/.claude/projects/*/*.jsonl を mtime 順で整理する。
    1. mtime が keep_days 日以前のものを削除
    2. それでも合計が max_bytes を超える場合は更に古い方から削除して quota 内に収める

    claude CLI の会話ログは turn ごとに append され、 /clear で新ファイルが切られるが、
    自動 cleanup が無いので無限に蓄積する (= 実機で 468 MB / 168 ファイルの蓄積を確認)。"""
    base = Path("~/.claude/projects/").expanduser()
    if not base.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    deleted = 0
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        files: list[tuple[Path, float, int]] = []
        for f in proj_dir.glob("*.jsonl"):
            try:
                st = f.stat()
                files.append((f, st.st_mtime, st.st_size))
            except OSError:
                continue
        if not files:
            continue
        files.sort(key=lambda x: x[1])  # mtime 古い順
        # Step 1: keep_days より古いものを削除
        survivors: list[tuple[Path, float, int]] = []
        for f, mt, sz in files:
            if mt < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                    logger.info(
                        "jsonl gc: removed by age %s (age=%.1fd, size=%dKB)",
                        f.name, (time.time() - mt) / 86400, sz // 1024,
                    )
                    continue
                except OSError:
                    pass
            survivors.append((f, mt, sz))
        # Step 2: 残量が quota 超なら古い方から削除
        survivors.sort(key=lambda x: x[1])
        total = sum(sz for _, _, sz in survivors)
        for f, _mt, sz in survivors:
            if total <= max_bytes:
                break
            try:
                f.unlink()
                total -= sz
                deleted += 1
                logger.info(
                    "jsonl gc: removed by quota %s (size=%dKB, remaining=%dMB)",
                    f.name, sz // 1024, total // (1024 * 1024),
                )
            except OSError:
                pass
    if deleted:
        logger.info("jsonl gc: total %d files deleted", deleted)
    return deleted


_FOOTPRINT_UNITS = {"B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2,
                    "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4}
_FOOTPRINT_RE = re.compile(r"phys_footprint:\s*([\d.]+)\s*([KMGT]?B)", re.IGNORECASE)


def _pgrep_one(pattern: str, *, exact: bool = False) -> int | None:
    """pattern にマッチする最初の pid を返す (無ければ None)。 exact=True は -x (完全一致)。"""
    args = ["pgrep", "-x" if exact else "-f", pattern]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    lines = [ln for ln in r.stdout.split() if ln.strip()]
    return int(lines[0]) if lines else None


def _phys_footprint_bytes(pid: int) -> int | None:
    """`footprint <pid>` を parse して phys_footprint をバイトで返す。 リークは swap に
    退避されて RSS には出ないため、 swap/compressed も計上する phys_footprint で測る。"""
    try:
        r = subprocess.run(["footprint", str(pid)], capture_output=True, text=True, timeout=5.0)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    m = _FOOTPRINT_RE.search(r.stdout)
    if not m:
        return None
    return int(float(m.group(1)) * _FOOTPRINT_UNITS[m.group(2).upper()])


def restart_sunshine_if_bloated() -> bool:
    """Sunshine が phys_footprint 閾値を超えて肥大化し、 かつアクティブな画面共有
    ストリームが無い時だけ kill -9 で restart する (= LaunchAgent KeepAlive が clean
    respawn)。 配信中 (= moonlight streamer プロセス在席) は使用中のペアを壊さないよう
    必ずスキップ。 Sunshine 未導入機では no-op。

    kill -9 を使う理由: SIGTERM (launchctl kickstart) 経由の graceful shutdown は
    ScreenCaptureKit / VideoToolbox の resource を中途半端に解放し respawn 後の encoder
    初期化を hang させる既知の地雷がある。 SIGKILL なら OS が resource を強制 reap する。"""
    pid = _pgrep_one("sunshine", exact=True)
    if pid is None:
        return False  # 未導入 or 停止中
    # 配信中なら触らない (= moonlight-web-stream が stream ごとに streamer を spawn)
    if _pgrep_one("release/streamer") is not None:
        return False
    footprint = _phys_footprint_bytes(pid)
    if footprint is None or footprint <= SUNSHINE_FOOTPRINT_MAX_BYTES:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    logger.info(
        "maintenance: restarted bloated sunshine pid=%d (phys_footprint=%.1fGB > %.1fGB)",
        pid, footprint / 1024**3, SUNSHINE_FOOTPRINT_MAX_BYTES / 1024**3,
    )
    return True


def run_all_maintenance() -> dict:
    """全 cleanup を 1 回実行 + 結果サマリを返す。 起動時と定期 loop の両方で呼ぶ。"""
    return {
        "killed_tmux": cleanup_stale_tmux_sessions(),
        "removed_statusline_map": cleanup_stale_statusline_map(),
        "removed_jsonl": cleanup_old_jsonl(),
        "restarted_sunshine": restart_sunshine_if_bloated(),
    }


async def maintenance_loop(interval_sec: int = MAINTENANCE_INTERVAL_SEC) -> None:
    """定期 maintenance: interval_sec ごとに全 cleanup を実行。"""
    logger.info("maintenance_loop started (interval=%ds)", interval_sec)
    try:
        while True:
            try:
                await asyncio.sleep(interval_sec)
                summary = run_all_maintenance()
                logger.info("maintenance tick: %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("maintenance_loop iteration failed")
    except asyncio.CancelledError:
        logger.info("maintenance_loop cancelled")
        raise
