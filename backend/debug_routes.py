"""App (iOS native app) からのデバッグログ POST 受け口。

iOS 18 + libimobiledevice 環境で NSLog が idevicesyslog に relay されない問題の
回避策。 アプリ側から重要イベント (NvHTTP の URL/レスポンス、 stage 進行等) を
HTTP POST で送ってもらい、 backend 側でファイルに集約 → ARK が tail / read で読む。

ログは `/tmp/app-debug.log` に追記。 サイズ上限は持たない (= 必要なら定期削除)。
"""
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Body

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_PATH = Path("/tmp/app-debug.log")


@router.post("/debug/log")
async def haven_log(payload: dict = Body(...)):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = str(payload.get("tag", "log"))[:32]
    msg = str(payload.get("message", ""))
    line = f"{ts} [{tag}] {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        logger.debug("app debug log write failed", exc_info=True)
    return {"ok": True}


@router.delete("/debug/log")
async def haven_log_clear():
    """ログを丸ごと消す (新セッションを始めたい時に呼ぶ)。"""
    try:
        LOG_PATH.write_text("")
    except Exception:
        logger.debug("app debug log clear failed", exc_info=True)
    return {"ok": True}
