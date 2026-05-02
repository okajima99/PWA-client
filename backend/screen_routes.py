"""デスクトップ画面 + system audio を WebRTC で PWA 側に live 配信するルータ。

PWA から /screen/offer に SDP offer を投げると backend が aiortc で
PeerConnection を立てて MediaPlayer 経由でトラックを送る。

**取り込み経路は ffmpeg subprocess + matroska pipe**:
- ffmpeg が avfoundation を叩いて video (yuv420p rawvideo) + audio (opus) を
  matroska でまとめて stdout に流す
- PyAV (aiortc.MediaPlayer) は pipe から matroska として読み込む
- これで PyAV は avfoundation を直接触らず、 macOS TCC の Screen Recording 権限は
  ffmpeg だけで足りる (= launchd 経由で起動された python に権限が継承されない問題を回避)

- 画面: avfoundation の screen capture device (config.json screen_share.video_device)
- 音声: BlackHole 等の仮想入力経由で system audio を吸う (screen_share.audio_device)

単一接続前提: 同時 1 peer のみ。 既存があれば teardown して新しいものに置き換える。
config.json で screen_share.enabled が false (or 未設定) の場合は 503 で拒否する。
"""
import asyncio
import logging
import shutil
import subprocess
import threading
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer
    from aiortc.mediastreams import MediaStreamTrack
    _HAS_AIORTC = True
except ImportError:
    _HAS_AIORTC = False
    RTCPeerConnection = None  # type: ignore
    RTCSessionDescription = None  # type: ignore
    MediaPlayer = None  # type: ignore
    MediaStreamTrack = object  # type: ignore


class _Yuv420Track(MediaStreamTrack):  # type: ignore[misc]
    """MediaPlayer.video を包んで、 frame を tight-stride yuv420p に再アロケートしてから送る。

    崩れの原因として、 avfoundation が返す frame の row stride が width と一致せず
    アライメント分の padding が乗ってる可能性が高い。 padding 込みのバッファを
    aiortc/VP8 encoder にそのまま渡すと「この行は width だけ読む」 想定で読まれて
    横ズレ → magenta / 横縞ノイズになる。

    `reformat(width, height, format)` を明示指定すると PyAV が新規バッファを確保し
    tight stride (= 各行の bytes が width にぴったり) で出してくれるので、 encoder の
    解釈ミスが起きない。 さらに width/height を 16 の倍数に丸めて、 VP8 のマクロブロック
    境界とも揃える。
    """

    kind = "video"

    def __init__(self, source):
        super().__init__()
        self._source = source
        self._out_w = None
        self._out_h = None

    async def recv(self):
        frame = await self._source.recv()
        if self._out_w is None:
            # 16 の倍数に切り下げる (= VP8 マクロブロック境界に揃える)
            self._out_w = (frame.width // 16) * 16 or frame.width
            self._out_h = (frame.height // 16) * 16 or frame.height
            # 診断ログ: source frame の生情報を出す
            try:
                src_planes = []
                for i, p in enumerate(frame.planes):
                    src_planes.append(f"plane{i}(stride={p.line_size},buffer={p.buffer_size})")
                logger.error(
                    "[SCREEN-DIAG] SOURCE width=%d height=%d format=%s pts=%s time_base=%s %s",
                    frame.width, frame.height, frame.format.name,
                    frame.pts, frame.time_base, " ".join(src_planes),
                )
            except Exception:
                logger.exception("[SCREEN-DIAG] source planes log failed")
        new_frame = frame.reformat(width=self._out_w, height=self._out_h, format="yuv420p")
        # 診断ログ: 最初のフレームのみ
        if not getattr(self, "_logged_out", False):
            try:
                out_planes = []
                for i, p in enumerate(new_frame.planes):
                    out_planes.append(f"plane{i}(stride={p.line_size},buffer={p.buffer_size})")
                logger.error(
                    "[SCREEN-DIAG] OUTPUT width=%d height=%d format=%s %s",
                    new_frame.width, new_frame.height, new_frame.format.name,
                    " ".join(out_planes),
                )
            except Exception:
                logger.exception("[SCREEN-DIAG] output planes log failed")
            self._logged_out = True
        new_frame.pts = frame.pts
        new_frame.time_base = frame.time_base
        return new_frame

    def stop(self):
        try:
            self._source.stop()
        except Exception:
            pass
        super().stop()

from config import SCREEN_SHARE

logger = logging.getLogger(__name__)
router = APIRouter()

# 単一接続: グローバルに 1 組保持
_pc: Optional["RTCPeerConnection"] = None
_player: Optional["MediaPlayer"] = None
_ffmpeg: Optional[subprocess.Popen] = None
_lock = asyncio.Lock()


def _enabled() -> bool:
    return _HAS_AIORTC and bool(SCREEN_SHARE.get("enabled"))


async def _teardown() -> None:
    """既存 peer / player / ffmpeg subprocess を停止する。 lock 内で呼ぶこと。

    subprocess.wait は最大 2 秒 block するので `asyncio.to_thread` 経由で呼んで
    event loop を解放する。
    """
    global _pc, _player, _ffmpeg
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
    if _ffmpeg is not None:
        proc = _ffmpeg
        _ffmpeg = None
        try:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 2)
                except Exception:
                    pass
        except Exception:
            logger.exception("ffmpeg terminate failed")


async def shutdown() -> None:
    """lifespan 終了時に呼ばれる。 既存接続を全てクリーンアップ。"""
    async with _lock:
        await _teardown()


def _ffmpeg_path() -> str:
    """ffmpeg バイナリのパスを解決。 PATH に無ければ Homebrew default を試す。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    fallback = "/opt/homebrew/bin/ffmpeg"
    return fallback


def _drain_stderr(proc: subprocess.Popen) -> None:
    """ffmpeg の stderr を背景で読みつつ logger に流す (バッファ満杯による block 防止)。"""
    try:
        for line in iter(proc.stderr.readline, b""):
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.error("[ffmpeg] %s", text)
    except Exception:
        logger.exception("ffmpeg stderr drain failed")


def _spawn_ffmpeg() -> subprocess.Popen:
    """avfoundation を ffmpeg で読んで、 stdout に matroska (rawvideo + opus) を流す。"""
    cfg = SCREEN_SHARE
    video_device = cfg.get("video_device", "1")
    audio_device = cfg.get("audio_device", "0")
    framerate = str(cfg.get("framerate", 30))
    video_size = cfg.get("video_size", "1280x800")

    if audio_device in (None, "", "none"):
        input_arg = f"{video_device}:none"
        audio_args = ["-an"]
    else:
        input_arg = f"{video_device}:{audio_device}"
        audio_args = [
            "-c:a", "libopus",
            "-b:a", "64k",
            "-ar", "48000",
            "-ac", "2",
        ]

    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        # input
        "-f", "avfoundation",
        "-framerate", framerate,
        "-video_size", video_size,
        "-capture_cursor", "1",
        "-pixel_format", "uyvy422",
        "-i", input_arg,
        # video filter: yuv420p (encoder 互換) + 16 倍数アライメント
        "-vf", "format=yuv420p,scale=trunc(iw/16)*16:trunc(ih/16)*16",
        "-c:v", "rawvideo",
        *audio_args,
        # output container: matroska は streaming 可能で video+audio をまとめられる
        "-f", "matroska",
        "pipe:1",
    ]
    logger.error("[SCREEN-DIAG] spawning ffmpeg: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10 * 1024 * 1024,
    )
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()
    return proc


import time as _time


def _build_player_sync() -> tuple:
    """ffmpeg subprocess を立ち上げて、 そこから matroska を PyAV で読み込む。
    返り値: (MediaPlayer, subprocess.Popen)

    **同期 (sync) 関数**。 av.open() が container header 待ちで read を block するため、
    呼び出し側は `asyncio.to_thread()` でラップして event loop を解放すること。

    ffmpeg が device エラー等で即死する場合に長時間 hang しないよう、 spawn 直後に
    300ms だけ poll して proc が既に exited なら即座に例外を投げる (fast-fail)。
    """
    cfg = SCREEN_SHARE
    if cfg.get("video_device") is None:
        raise RuntimeError("screen_share.video_device が未設定です")
    proc = _spawn_ffmpeg()
    # fast-fail: ffmpeg が起動失敗 (Invalid device index 等) なら即捕まえる
    deadline = _time.monotonic() + 0.3
    while _time.monotonic() < deadline:
        ret = proc.poll()
        if ret is not None:
            raise RuntimeError(
                f"ffmpeg exited immediately (code={ret}); device 設定を確認してください"
            )
        _time.sleep(0.03)

    try:
        # MediaPlayer は av.open(file=proc.stdout, format='matroska') で初期化される。
        # ffmpeg がまだ header を吐いてない瞬間は read で待たされるが、
        # 上の fast-fail で生きていることは確認済みなので ms 単位で抜ける想定。
        player = MediaPlayer(proc.stdout, format="matroska")
        return player, proc
    except Exception:
        # PyAV 起動失敗時は ffmpeg を必ず止める
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise


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

    global _pc, _player, _ffmpeg

    # === 1. 重い処理 (ffmpeg spawn + av.open) を thread executor に逃がす ===
    # event loop を block しないため。 失敗時はこの中で ffmpeg を始末済み。
    try:
        player, proc = await asyncio.to_thread(_build_player_sync)
    except Exception as e:
        logger.exception("MediaPlayer build failed")
        raise HTTPException(status_code=500, detail=f"capture init failed: {e}")

    # === 2. peer 生成 + ICE gathering まで lock 外で進める ===
    pc = RTCPeerConnection()  # No ICE servers; LAN 直結 (Tailscale) 前提

    @pc.on("connectionstatechange")
    async def _on_state_change():
        logger.info("screen pc state=%s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            asyncio.create_task(_async_cleanup_if(pc))

    if player.video is not None:
        pc.addTrack(_Yuv420Track(player.video))
    if player.audio is not None:
        pc.addTrack(player.audio)

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=typ))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await _wait_ice_gathering_complete(pc)
    except Exception as e:
        logger.exception("offer handling failed")
        # ローカル resource を thread で片付ける (block しないように)
        try:
            await pc.close()
        except Exception:
            pass
        try:
            for t in (player.video, player.audio):
                if t is not None:
                    t.stop()
        except Exception:
            pass
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 2)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"offer handling failed: {e}")

    # === 3. ここまで来たら全部できた → 短時間 lock を取って globals に install ===
    async with _lock:
        await _teardown()  # 旧 peer / player / ffmpeg を片付け
        _pc = pc
        _player = player
        _ffmpeg = proc

        # 診断: negotiated codec を SDP answer から拾って log
        try:
            ans_sdp = pc.localDescription.sdp
            for line in ans_sdp.splitlines():
                if line.startswith("a=rtpmap:") and ("/" in line):
                    logger.error("[SCREEN-DIAG] negotiated rtpmap: %s", line)
        except Exception:
            logger.exception("[SCREEN-DIAG] codec log failed")

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
