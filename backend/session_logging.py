"""セッション (= UI 上の 1 タブ) ごとに分かれた append-only デバッグログ。

`logs/sessions/<session_id>.log` にセッション固有の wire / msg / drain 等の足跡を
書き出す。 「セッション終了」 ボタン押下時にマーカー行 (`=== SESSION END ... ===`) を
入れて区切りとし、 2 セッション前以前 (= 最新 2 セッション分より古い) を prune する。

呼び出し側は `session_log(sid, "...")` を logger.info 代わりに使うイメージ。

ファイルハンドルは `_handles` にキャッシュして毎回の open/close を避ける。
セッション切断・削除のタイミングで `close_session_log` を呼んで解放する。
"""
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_ROOT = Path(__file__).parent.parent / "logs" / "sessions"
LOG_ROOT.mkdir(parents=True, exist_ok=True)

# 「セッション終了」 を区切る一意な行頭プリフィックス。 検索しやすく、 通常ログとぶつからない
SESSION_END_PREFIX = "=== SESSION END "
# 1 タブが「現在 + 1 個前」 の 2 セッションぶんまで保持する
KEEP_SESSIONS = 2

# 1 セッションログの上限 (= bytes)。 超えたら頭から N% を捨てて末尾を残す (= 直近を見たい
# 用途に最適化)。 KEEP_SESSIONS = 2 と合わせて 1 タブあたりの最大容量を bound する
# (= 上限 ~5MB / session × 2 = 10MB)。
SESSION_LOG_MAX_BYTES = 5 * 1024 * 1024
# 上限到達時に切り詰める割合 (= 0.5 なら前半 50% を捨てる)。 頻繁な trim を避けるため
# 大きめに削る。
SESSION_LOG_TRIM_RATIO = 0.5

# session_id → 開きっぱなしの append-mode file handle
# 同 backend プロセス (シングル) で使うので排他制御は不要、 GIL 内で逐次 write される
_handles: dict[str, "object"] = {}
# session_id → 直近サイズチェック以降に書いたバイト数 (= 毎 write で stat する代わりに、
# 増分が一定を超えた時だけ実 stat を取って trim 判定する)
_write_since_check: dict[str, int] = {}
# stat を取るかを決める閾値 (= 100KB 書く毎に 1 回 stat)。 サイズ計算と open/close の
# オーバーヘッドを抑えるための間引き。
_STAT_CHECK_INTERVAL_BYTES = 100 * 1024


def _path_for(session_id: str) -> Path:
    # session_id は内部生成 (ses_xxxx) なのでパス事故は無いが保険として basename 化
    safe = session_id.replace("/", "_").replace("\\", "_")
    return LOG_ROOT / f"{safe}.log"


def _get_handle(session_id: str):
    h = _handles.get(session_id)
    if h is not None and not getattr(h, "closed", True):
        return h
    try:
        h = _path_for(session_id).open("a", encoding="utf-8", buffering=1)
        _handles[session_id] = h
        return h
    except Exception:
        logger.exception("failed to open session log for %s", session_id)
        return None


def _drop_handle(session_id: str) -> None:
    h = _handles.pop(session_id, None)
    _write_since_check.pop(session_id, None)
    if h is None:
        return
    try:
        h.close()
    except Exception:
        pass


def _maybe_trim(session_id: str) -> None:
    """書き込み済バイト数が一定を超えたら実 stat を取り、 ファイルが SESSION_LOG_MAX_BYTES
    を超えてたら頭から TRIM_RATIO 分を捨てる。 stat は毎 write で取らず間引く。"""
    written = _write_since_check.get(session_id, 0)
    if written < _STAT_CHECK_INTERVAL_BYTES:
        return
    _write_since_check[session_id] = 0
    p = _path_for(session_id)
    try:
        size = p.stat().st_size
    except Exception:
        return
    if size <= SESSION_LOG_MAX_BYTES:
        return
    # 上限超過: 頭から TRIM_RATIO 分を捨てる。 ハンドルを閉じてから一気に読み出して
    # 末尾 (1 - TRIM_RATIO) を新 file に書き直す (= atomic に replace)。 行境界で
    # 切るために最初の '\n' まで読み飛ばす。
    _drop_handle(session_id)
    try:
        data = p.read_bytes()
    except Exception:
        logger.exception("session-log trim read failed for %s", session_id)
        return
    cut_at = int(len(data) * SESSION_LOG_TRIM_RATIO)
    nl = data.find(b"\n", cut_at)
    if nl >= 0:
        cut_at = nl + 1
    new_bytes = data[cut_at:]
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(new_bytes)
        tmp.replace(p)
    except Exception:
        logger.exception("session-log trim write failed for %s", session_id)


def session_log(session_id: str, line: str) -> None:
    """1 行追記。 line に改行文字は含まない前提 (内部で 1 行 = 1 イベント)。
    open はキャッシュされたハンドル経由で行うので毎回の open/close コストは無い。
    `buffering=1` (line buffering) で 1 行ごとに flush されるため、
    プロセス kill 時のロスは最大 1 行。
    write 量を蓄積して一定バイトごとに _maybe_trim を呼び、 上限超えてたら頭を切り詰める。"""
    if not session_id:
        return
    h = _get_handle(session_id)
    if h is None:
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = f"{ts} {line}\n"
        h.write(payload)
    except Exception:
        logger.exception("session_log write failed for session=%s", session_id)
        # 壊れたハンドルは捨てる (次回 open し直す)
        _drop_handle(session_id)
        return
    _write_since_check[session_id] = _write_since_check.get(session_id, 0) + len(payload.encode("utf-8"))
    _maybe_trim(session_id)


def mark_session_end(session_id: str) -> None:
    """セッション終了の境界マーカーを 1 行入れる。 prune の区切りとして読まれる。"""
    if not session_id:
        return
    h = _get_handle(session_id)
    if h is None:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        h.write(f"{SESSION_END_PREFIX}{ts} ===\n")
    except Exception:
        logger.exception("mark_session_end failed for session=%s", session_id)
        _drop_handle(session_id)


def prune_session_log(session_id: str, keep: int = KEEP_SESSIONS) -> None:
    """ファイル内のマーカー数を見て、 末尾 `keep` セッションぶんより古い行を捨てる。

    マーカーは「セッション終了」 1 個 = 1 個前のセッションが終わったことを示す。
    keep=2 なら「現在進行中のセッション + 直前に終了した 1 セッション」 が残る。
    """
    p = _path_for(session_id)
    if not p.exists():
        return
    # 一時的にハンドルを閉じる (read+rewrite するため)
    _drop_handle(session_id)
    try:
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        logger.exception("prune read failed for session=%s", session_id)
        return

    threshold_index = -1  # ここより前を削除
    found = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(SESSION_END_PREFIX):
            found += 1
            if found == keep:
                threshold_index = i
                break

    if threshold_index <= 0:
        return

    new_content = "".join(lines[threshold_index + 1:])
    try:
        # 一時ファイルに書いてから差し替え (atomic)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.exception("prune write failed for session=%s", session_id)


def close_session_log(session_id: str) -> None:
    """セッションが idle disconnect / 削除された時にハンドルを閉じる。"""
    _drop_handle(session_id)


def delete_session_log(session_id: str) -> None:
    """セッション削除時に丸ごと unlink。 ハンドルも閉じる。"""
    _drop_handle(session_id)
    p = _path_for(session_id)
    try:
        p.unlink(missing_ok=True)
        # tmp が残ってる稀ケースも掃除
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.unlink(missing_ok=True)
    except Exception:
        logger.exception("delete_session_log failed for session=%s", session_id)


def prune_all_existing(session_ids: list[str]) -> None:
    """起動時の 1 回掃除: 現存する session_id すべてに対して prune を試行。"""
    for sid in session_ids:
        prune_session_log(sid)


def close_all() -> None:
    """シャットダウン時に全ハンドルを閉じる (lifespan の cleanup から呼ぶ)。"""
    for sid in list(_handles.keys()):
        _drop_handle(sid)
