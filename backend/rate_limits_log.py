"""Anthropic API 1 turn ごとの usage / rate-limit を JSONL に永続化する観測 sink。

Anthropic API レスポンスから ResultMessage を受け取った時点で、 その時の
shared_status (= 5h / 7d 使用率 + reset 時刻) と AssistantMessage 集計の
usage (= input_tokens / output_tokens / cache_read / cache_creation) を 1 行
1 JSON で append する。 PWA 経由の token 消費を時系列で観察するための一次情報。

旧版は 2026-05-07 に書き込み経路ごと消えていた (= rate-limits.jsonl の更新が
止まっていた)。 復活版では:

- ファイル上限を MAX_BYTES に bound、 超えたら頭から TRIM_RATIO を捨てる
  (= 末尾の直近データを残す、 長期運用しても容量が単調増加しない)
- 毎 append で stat を読むのは I/O が重いので、 CHECK_INTERVAL 回ごとにだけ
  size 確認する間引き
- 例外は debug log にだけ落として握りつぶす (= 本筋の SDK turn 処理を観測 sink
  の失敗で巻き込まない)
"""
import json
import logging
from pathlib import Path
from typing import Any

from config import RATE_LIMITS_LOG_PATH

logger = logging.getLogger(__name__)

# 1 ファイル上限 (= bytes)。 5MB 1 ファイルで bound、 配布前提の容量自動 bounded 流。
MAX_BYTES = 5 * 1024 * 1024
# 上限到達時に頭から捨てる割合 (= 0.5 なら前半 50% を捨てる)。 頻繁な trim を避ける
TRIM_RATIO = 0.5
# 毎 N 回の append 後にだけ stat を読む (= I/O 削減)
CHECK_INTERVAL = 100

_write_count = 0


def append(entry: dict[str, Any]) -> None:
    """1 エントリ追加。 RATE_LIMITS_LOG_PATH が空 (= config 未設定) なら no-op。"""
    if not RATE_LIMITS_LOG_PATH:
        return
    try:
        path = Path(RATE_LIMITS_LOG_PATH).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a") as f:
            f.write(line)
        global _write_count
        _write_count += 1
        if _write_count % CHECK_INTERVAL == 0:
            _maybe_trim(path)
    except Exception:
        logger.debug("rate_limits_log append failed", exc_info=True)


def _maybe_trim(path: Path) -> None:
    """ファイルが MAX_BYTES を超えてたら頭から TRIM_RATIO の量を改行境界まで切り捨てる。"""
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        data = path.read_bytes()
        cut_at = int(len(data) * TRIM_RATIO)
        newline = data.find(b"\n", cut_at)
        if newline == -1:
            return
        path.write_bytes(data[newline + 1:])
    except Exception:
        logger.debug("rate_limits_log trim failed", exc_info=True)
