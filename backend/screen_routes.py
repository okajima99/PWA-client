"""デスクトップ画面 + system audio を WebRTC で PWA 側に live 配信するルータ。

PWA から /screen/offer に SDP offer を投げると backend が aiortc で
PeerConnection を立てて MediaPlayer (avfoundation) のトラックを送る。

- 画面: avfoundation の screen capture device (config.json screen_share.video_device)
- 音声: BlackHole 等の仮想入力経由で system audio を吸う (screen_share.audio_device)

単一接続前提: 同時 1 peer のみ。 既存があれば teardown して新しいものに置き換える。
config.json で screen_share.enabled が false (or 未設定) の場合は 503 で拒否する。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer
    _HAS_AIORTC = True
except ImportError:
    _HAS_AIORTC = False
    RTCPeerConnection = None  # type: ignore
    RTCSessionDescription = None  # type: ignore
    MediaPlayer = None  # type: ignore

from config import SCREEN_SHARE

logger = logging.getLogger(__name__)
router = APIRouter()

# 単一接続: グローバルに 1 組保持
_pc: Optional["RTCPeerConnection"] = None
_player: Optional["MediaPlayer"] = None
_lock = asyncio.Lock()


def _enabled() -> bool:
    return _HAS_AIORTC and bool(SCREEN_SHARE.get("enabled"))


async def _teardown() -> None:
    """既存 peer / player を停止する。 lock 内で呼ぶこと。"""
    global _pc, _player
    if _pc is not None:
        try:
            await _pc.close()
        except Exception:
            logger.exception("pc.close failed")
        _pc = None
    if _player is not None:
        try:
            for track_attr in ("video", "audio"):
                t = getattr(_player, track_attr, None)
                if t is not None:
                    try:
                        t.stop()
                    except Exception:
                        pass
        finally:
            _player = None


async def shutdown() -> None:
    """lifespan 終了時に呼ばれる。 既存接続を全てクリーンアップ。"""
    async with _lock:
        await _teardown()


def _build_player() -> "MediaPlayer":
    cfg = SCREEN_SHARE
    video_device = cfg.get("video_device")
    audio_device = cfg.get("audio_device")
    if video_device is None:
        raise RuntimeError("screen_share.video_device が未設定です")
    # avfoundation の入力 URL: "<video>:<audio>" 形式。 audio 不要なら "<video>:none"
    if audio_device:
        input_url = f"{video_device}:{audio_device}"
    else:
        input_url = f"{video_device}:none"
    options = {
        "framerate": str(cfg.get("framerate", 30)),
        "video_size": cfg.get("video_size", "1600x1000"),
        # capture_cursor=1 でマウスカーソルも一緒に映す
        "capture_cursor": "1",
    }
    logger.info("starting MediaPlayer input=%r options=%s", input_url, options)
    return MediaPlayer(input_url, format="avfoundation", options=options)


@router.get("/screen/status")
async def screen_status():
    state = "idle"
    if _pc is not None:
        state = _pc.connectionState
    return {
        "enabled": _enabled(),
        "connected": _pc is not None and state in ("connected", "connecting", "new", "checking"),
        "state": state,
    }


@router.post("/screen/offer")
async def screen_offer(payload: dict = Body(...)):
    if not _HAS_AIORTC:
        raise HTTPException(
            status_code=503,
            detail="aiortc が未インストールです。 pip install aiortc を実行してください",
        )
    if not _enabled():
        raise HTTPException(status_code=503, detail="screen_share が config で有効化されていません")

    sdp = payload.get("sdp")
    typ = payload.get("type")
    if not isinstance(sdp, str) or typ != "offer":
        raise HTTPException(status_code=400, detail="invalid offer")

    global _pc, _player

    async with _lock:
        # 既存があれば teardown して後勝ち
        await _teardown()

        try:
            _player = _build_player()
        except Exception as e:
            logger.exception("MediaPlayer build failed")
            raise HTTPException(status_code=500, detail=f"capture init failed: {e}")

        pc = RTCPeerConnection()  # No ICE servers; LAN 直結 (Tailscale) 前提
        _pc = pc

        @pc.on("connectionstatechange")
        async def _on_state_change():
            logger.info("screen pc state=%s", pc.connectionState)
            if pc.connectionState in ("failed", "closed", "disconnected"):
                asyncio.create_task(_async_cleanup_if(pc))

        if _player.video is not None:
            pc.addTrack(_player.video)
        if _player.audio is not None:
            pc.addTrack(_player.audio)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=typ))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await _wait_ice_gathering_complete(pc)
        except Exception as e:
            logger.exception("offer handling failed")
            await _teardown()
            raise HTTPException(status_code=500, detail=f"offer handling failed: {e}")

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


async def _wait_ice_gathering_complete(pc: "RTCPeerConnection", timeout: float = 5.0) -> None:
    """non-trickle ICE: gathering 完了まで待ってから answer を返す。 タイムアウト時は警告だけ。"""
    if pc.iceGatheringState == "complete":
        return
    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    @pc.on("icegatheringstatechange")
    def _on():
        if pc.iceGatheringState == "complete" and not fut.done():
            fut.set_result(None)

    try:
        await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("ICE gathering timeout (%.1fs)", timeout)


async def _async_cleanup_if(target_pc: "RTCPeerConnection") -> None:
    """connectionstatechange callback から呼ばれる遅延 cleanup。
    現在保持中の _pc が target と一致する場合のみ teardown する (race 防止)。"""
    global _pc
    async with _lock:
        if _pc is target_pc:
            await _teardown()


@router.post("/screen/disconnect")
async def screen_disconnect():
    async with _lock:
        await _teardown()
    return {"ok": True}
