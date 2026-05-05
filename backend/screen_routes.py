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
import os
import shutil
import subprocess
import threading
from pathlib import Path
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
# 接続前の出力先 (例: AirPods) を覚えておいて、 切断時に戻す
_prev_audio_output: Optional[str] = None
_lock = asyncio.Lock()

# === Adaptive Bitrate Rate (ABR) controller ===
# 5 秒ごとに pc.getStats() を見て loss / RTT に応じて video sender の maxBitrate を上下。
# ヒステリシス 10 秒 (頻繁に変えない)。 Google Remote Desktop 風の挙動を簡易再現。
_abr_task: Optional[asyncio.Task] = None
_abr_target_kbps: int = 1000  # 現在の target bitrate (kbps)
_video_codec_label: str = "rawvideo"  # 表示用 (h264hw / vp8 / rawvideo)
ABR_INTERVAL_SEC = 5
ABR_HYSTERESIS_SEC = 10
ABR_MIN_KBPS = 300
ABR_MAX_KBPS = 1500    # 現実的な上限 (4G で安定運用、 LAN でも体感差は小さい)
ABR_LOSS_HIGH = 0.05   # 5% 超で下げる
ABR_LOSS_LOW = 0.01    # 1% 未満で上げる
ABR_RTT_OK_MS = 200    # RTT これ以下なら上げ判定対象


def _switchaudio_path() -> str:
    p = shutil.which("SwitchAudioSource")
    return p or "/opt/homebrew/bin/SwitchAudioSource"


def _get_current_audio_output() -> str:
    """現在の system 出力先デバイス名を返す。 取得失敗は空文字。"""
    try:
        res = subprocess.run(
            [_switchaudio_path(), "-c", "-t", "output"],
            capture_output=True, timeout=3, text=True,
        )
        return (res.stdout or "").strip()
    except Exception:
        logger.exception("get current audio output failed")
        return ""


def _switch_audio_output(device_name: str) -> None:
    """system 出力先を切り替える (best-effort、 失敗しても進行)。"""
    if not device_name:
        return
    try:
        subprocess.run(
            [_switchaudio_path(), "-s", device_name, "-t", "output"],
            capture_output=True, timeout=3,
        )
        logger.info("[audio-output] switched to %s", device_name)
    except Exception:
        logger.exception("audio output switch failed for %s", device_name)


def _enabled() -> bool:
    return _HAS_AIORTC and bool(SCREEN_SHARE.get("enabled"))


async def _teardown() -> None:
    """既存 peer / player / ffmpeg subprocess を停止する。 lock 内で呼ぶこと。

    subprocess.wait は最大 2 秒 block するので `asyncio.to_thread` 経由で呼んで
    event loop を解放する。 接続前に切り替えた音声出力先も復元する。
    """
    global _pc, _player, _ffmpeg, _prev_audio_output
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
    # 出力先を接続前の値に戻す
    if _prev_audio_output:
        prev = _prev_audio_output
        _prev_audio_output = None
        try:
            await asyncio.to_thread(_switch_audio_output, prev)
        except Exception:
            logger.exception("audio output switch (restore) failed")
    # ABR loop は次回接続まで止める (peer 無くなれば判定スキップだけど task は cancel)
    global _abr_task
    if _abr_task is not None and not _abr_task.done():
        _abr_task.cancel()
        try:
            await _abr_task
        except (Exception, asyncio.CancelledError):
            pass
        _abr_task = None


def kill_orphan_ffmpegs() -> int:
    """以前の backend インスタンスから孤児化した ffmpeg(avfoundation→matroska) を全 kill。

    launchctl kickstart -k で SIGTERM された旧 backend が lifespan cleanup を完走できないと、
    spawn した ffmpeg が孤児化して画面収録デバイスを掴んだまま残る。 新 backend が新しい
    ffmpeg を spawn しても、 同 device を競合して安定して取り込めない (= ontrack 発火せず
    PWA 側「接続中…」 で固まる症状)。 起動時に 1 回掃除する。

    判定は cmdline マッチ: ffmpeg + avfoundation + matroska + pipe:1。 他用途の ffmpeg
    (Discord / OBS / 録画系) には触れない。
    """
    import re
    pattern = re.compile(r"ffmpeg.*avfoundation.*matroska.*pipe:1")
    killed = 0
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, timeout=3, text=True,
        )
    except Exception:
        logger.exception("ps for orphan ffmpeg scan failed")
        return 0
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not pattern.search(line):
            continue
        try:
            pid_str, _ = line.split(None, 1)
            pid = int(pid_str)
        except Exception:
            continue
        # 自分の子は触らない (= 起動直後なので _ffmpeg はまだ None だが念のため)
        if _ffmpeg is not None and getattr(_ffmpeg, "pid", None) == pid:
            continue
        try:
            import os
            import signal
            # SIGTERM が効かない実例があるので即 SIGKILL。 起動時の掃除なので
            # graceful shutdown を待つ理由が無い。
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("orphan ffmpeg kill failed pid=%d", pid)
    if killed:
        logger.info("[orphan-ffmpeg] killed %d leftover process(es)", killed)
    return killed


async def shutdown() -> None:
    """lifespan 終了時に呼ばれる。 既存接続を全てクリーンアップ。"""
    global _abr_task
    if _abr_task is not None and not _abr_task.done():
        _abr_task.cancel()
        try:
            await _abr_task
        except (Exception, asyncio.CancelledError):
            pass
        _abr_task = None
    async with _lock:
        await _teardown()


# aiortc のバージョンによって sender が getParameters/setParameters を持たないことがある。
# 1.14 系には未実装、 1.15+ で追加された。 持ってない場合 ABR loop を即終了させて
# 例外 spam で CPU を食わないようにする。
_abr_unsupported_reason: Optional[str] = None


def _set_video_max_bitrate(target_kbps: int) -> bool:
    """video sender の encoder target_bitrate を直接書き換える。

    aiortc 1.14 の RTCRtpSender は public な setParameters を持たないが、 内部で
    `self.__encoder.target_bitrate = bitrate` (REMB feedback で) と同じパターンを
    使ってる。 同じ private mangled 属性 `_RTCRtpSender__encoder` を読み出して
    直接書き換えれば、 次フレームから新 bitrate が反映される。

    encoder は最初の frame 送信時に lazy 生成されるので、 None の場合は次回リトライ
    (unsupported flag は立てない)。 sender 自体が無いか attr が無い時のみ unsupported。
    """
    global _abr_unsupported_reason
    if _pc is None:
        return False
    for sender in _pc.getSenders():
        track = sender.track
        if track is None or getattr(track, "kind", None) != "video":
            continue
        # 公開 API があればそっちを優先 (aiortc 1.15+ 用、 forward compat)
        if hasattr(sender, "setParameters") and hasattr(sender, "getParameters"):
            try:
                params = sender.getParameters()
                if getattr(params, "encodings", None):
                    params.encodings[0].maxBitrate = target_kbps * 1000
                    res = sender.setParameters(params)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                    return True
            except Exception:
                pass  # fall through to private path
        # aiortc 1.14: private mangled attribute
        encoder = getattr(sender, "_RTCRtpSender__encoder", None)
        if encoder is None:
            return False  # 未生成 (まだ frame 送信前)、 次回リトライ
        if not hasattr(encoder, "target_bitrate"):
            _abr_unsupported_reason = (
                f"encoder ({type(encoder).__name__}) lacks target_bitrate attr"
            )
            logger.warning("[ABR] disabled: %s", _abr_unsupported_reason)
            return False
        try:
            encoder.target_bitrate = target_kbps * 1000
            return True
        except Exception:
            logger.exception("[ABR] target_bitrate set failed")
            return False
    return False


async def _abr_loop():
    """ABR 制御ループ。 _pc が active な間、 stats を見て maxBitrate を調整する。

    setParameters / encoder.target_bitrate が aiortc バージョンで使えない場合、
    最初の試行で _abr_unsupported_reason がセットされて以後 loop は即抜ける
    (例外 spam で CPU を食うのを防ぐ)。
    """
    global _abr_target_kbps
    last_change = 0.0
    last_packets_lost = 0
    last_packets_sent = 0
    while True:
        try:
            await asyncio.sleep(ABR_INTERVAL_SEC)
            if _abr_unsupported_reason is not None:
                # API 未対応が判明したら以後は何もしない (CPU 節約)
                return
            if _pc is None or _pc.connectionState != "connected":
                continue
            stats = await _pc.getStats()
            packets_sent = 0
            packets_lost = 0
            rtt = 0.0
            for s in stats.values():
                t = getattr(s, "type", None)
                kind = getattr(s, "kind", None)
                if t == "outbound-rtp" and kind == "video":
                    packets_sent = getattr(s, "packetsSent", 0) or 0
                elif t == "remote-inbound-rtp" and kind == "video":
                    packets_lost = getattr(s, "packetsLost", 0) or 0
                    rtt = float(getattr(s, "roundTripTime", 0) or 0)

            # ロス率は「直近の interval」 のみで判定 (累積値の差分から計算)
            d_sent = max(0, packets_sent - last_packets_sent)
            d_lost = max(0, packets_lost - last_packets_lost)
            last_packets_sent = packets_sent
            last_packets_lost = packets_lost

            if d_sent <= 0:
                continue
            loss = d_lost / d_sent
            rtt_ms = rtt * 1000

            now = _time.monotonic()
            if now - last_change < ABR_HYSTERESIS_SEC:
                continue

            new_target = _abr_target_kbps
            if loss > ABR_LOSS_HIGH:
                new_target = max(ABR_MIN_KBPS, int(_abr_target_kbps * 0.7))
            elif loss < ABR_LOSS_LOW and rtt_ms < ABR_RTT_OK_MS:
                new_target = min(ABR_MAX_KBPS, int(_abr_target_kbps * 1.2))

            if new_target != _abr_target_kbps:
                if _set_video_max_bitrate(new_target):
                    logger.info(
                        "[ABR] %d→%d kbps (loss=%.1f%% rtt=%.0fms d_sent=%d)",
                        _abr_target_kbps, new_target, loss * 100, rtt_ms, d_sent,
                    )
                    _abr_target_kbps = new_target
                    last_change = now
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[ABR] loop iteration error")


def _ffmpeg_path() -> str:
    """ffmpeg バイナリのパスを解決。

    優先順位:
      1. ~/Applications/ScreenCaptureFFmpeg.app/Contents/MacOS/ScreenCaptureFFmpeg
         (= scripts/install-ffmpeg-bundle.sh で生成される .app バンドル内 ffmpeg)
         macOS Tahoe で TCC Screen Recording entry が ad-hoc 署名の bare binary より
         安定するため、 存在すれば優先して使う。
      2. PATH 上の ffmpeg
      3. Homebrew default /opt/homebrew/bin/ffmpeg
    """
    bundled = (
        Path.home()
        / "Applications"
        / "ScreenCaptureFFmpeg.app"
        / "Contents"
        / "MacOS"
        / "ScreenCaptureFFmpeg"
    )
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
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


def _spawn_ffmpeg() -> tuple:
    """avfoundation を ffmpeg で読んで、 stdout にコンテナを流す。

    返り値: (subprocess.Popen, decode_required: bool, codec_label: str, container_format: str)

    config の `video_encode`:
      - "rawvideo" (default): aiortc が decode + 自前 sw VP8 encode。 container=matroska。 ~300-500ms 遅延 (sw encode が重い)
      - "vp8": ffmpeg で libvpx-vp8 encode + decode=False passthrough。 container=matroska。 ~200-300ms
      - "h264_videotoolbox": M2 hw H264 encode + decode=False passthrough。 container=mpegts。 ~80-150ms (本命)

    container を h264_videotoolbox だけ mpegts にする理由: matroska は H264 を **AVCC 形式**
    (length-prefix) で格納するが、 RTP H264 は **Annex-B 形式** (start-code prefix) を要求する
    (aiortc.H264Encoder.pack() も Annex-B 前提)。 mpegts は Annex-B native なので形式変換不要で
    iOS Safari が decode 成功する。 vp8 / rawvideo は AVCC/Annex-B の問題が無いので matroska 維持。
    """
    cfg = SCREEN_SHARE
    video_device = cfg.get("video_device", "1")
    audio_device = cfg.get("audio_device", "0")
    framerate = str(cfg.get("framerate", 30))
    video_size = cfg.get("video_size", "1024x640")
    encode_mode = (cfg.get("video_encode") or "rawvideo").lower()
    initial_bitrate = int(cfg.get("initial_bitrate_kbps", 1000))

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

    # video encoder 切替
    if encode_mode == "h264_videotoolbox":
        # M2 のメディアエンジンで H264 hw encode。 sw encode の Python ボトルネックを除去。
        # iOS Safari 互換: profile=baseline 強制 (videotoolbox はデフォで Main/High を吐く、
        # SDP ミスマッチで黒画面になるため)。 -bf 0 は WebRTC の前提 (B-frame 禁止)。
        # **`-level:v` は明示しない**: 解像度 / fps に対して level が不足だと encoder が
        # -12902 で開けない (例: level 3.1 は max 1280x720@30fps なので 1600x1000 で死ぬ)。
        # videotoolbox に自動選択させて、 iOS Safari 側は profile-level-id ミスマッチを
        # 大抵許容する (実装依存だが多くのケースで OK)。
        video_args = [
            "-c:v", "h264_videotoolbox",
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline",
            "-realtime", "1",
            "-allow_sw", "1",       # hw encoder 失敗時に sw fallback
            "-b:v", f"{initial_bitrate}k",
            "-maxrate", f"{int(initial_bitrate * 1.5)}k",
            "-g", "30",              # keyframe 1 秒に 1 回 (PLI 即時 IDR は v1 では非実装、
                                     # 1 秒以内自動復旧で代替する)
            "-bf", "0",
        ]
        decode_required = False
        codec_label = "h264hw"
        # mpegts は Annex-B native なので RTP H264 packetizer (= aiortc.H264Encoder.pack)
        # に直接渡せる。 matroska だと AVCC で来るので別変換が必要 → 詰む。
        container_format = "mpegts"
    elif encode_mode == "vp8":
        video_args = [
            "-c:v", "libvpx",
            "-deadline", "realtime",
            "-cpu-used", "8",
            "-threads", "4",
            "-b:v", f"{initial_bitrate}k",
            "-g", "30",
            "-quality", "realtime",
        ]
        decode_required = False
        codec_label = "vp8"
        container_format = "matroska"
    else:
        # rawvideo (現状互換)
        video_args = ["-c:v", "rawvideo"]
        decode_required = True
        codec_label = "rawvideo"
        container_format = "matroska"

    cmd = [
        _ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        # input: pixel_format は指定しない (avfoundation の screen capture device は
        # uyvy422 を強制すると "Invalid device index" 系で開けないことがある)
        "-f", "avfoundation",
        "-framerate", framerate,
        "-video_size", video_size,
        "-capture_cursor", "1",
        "-i", input_arg,
        # video filter: yuv420p (encoder 互換) + 16 倍数アライメント
        "-vf", "format=yuv420p,scale=trunc(iw/16)*16:trunc(ih/16)*16",
        *video_args,
        *audio_args,
        # output container: encode_mode によって切替 (h264hw は mpegts、 他は matroska)
        "-f", container_format,
        "pipe:1",
    ]
    logger.error("[SCREEN-DIAG] spawning ffmpeg (encode=%s container=%s): %s",
                 encode_mode, container_format, " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10 * 1024 * 1024,
    )
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()
    return proc, decode_required, codec_label, container_format


import time as _time


def _build_player_sync() -> tuple:
    """ffmpeg subprocess を立ち上げて、 そこからコンテナを PyAV で読み込む。
    返り値: (MediaPlayer, subprocess.Popen, codec_label)

    **同期 (sync) 関数**。 av.open() が container header 待ちで read を block するため、
    呼び出し側は `asyncio.to_thread()` でラップして event loop を解放すること。

    ffmpeg が device エラー等で即死する場合に長時間 hang しないよう、 spawn 直後に
    300ms だけ poll して proc が既に exited なら即座に例外を投げる (fast-fail)。

    container は encode_mode に応じて自動選択される (h264hw=mpegts、 他=matroska)。
    """
    cfg = SCREEN_SHARE
    if cfg.get("video_device") is None:
        raise RuntimeError("screen_share.video_device が未設定です")
    proc, decode_required, codec_label, container_format = _spawn_ffmpeg()
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
        # decode=False で encoded packet を直接 aiortc に passthrough (h264hw / vp8 path)
        # decode=True (default) なら frame を decode して aiortc が再 encode (rawvideo path)
        player = MediaPlayer(proc.stdout, format=container_format, decode=decode_required)
        return player, proc, codec_label
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
    global _video_codec_label
    try:
        player, proc, codec_label = await asyncio.to_thread(_build_player_sync)
        _video_codec_label = codec_label
    except Exception as e:
        logger.exception("MediaPlayer build failed")
        raise HTTPException(status_code=500, detail=f"capture init failed: {e}")

    # === 1.5. 出力先デバイスを「マルチ出力 (BlackHole + speaker)」 に切替 ===
    # 元の出力先 (AirPods 等) を覚えておいて _teardown で戻す。
    audio_active = SCREEN_SHARE.get("audio_output_active")
    if audio_active:
        global _prev_audio_output
        try:
            current = await asyncio.to_thread(_get_current_audio_output)
            if current and current != audio_active:
                _prev_audio_output = current
                await asyncio.to_thread(_switch_audio_output, audio_active)
        except Exception:
            logger.exception("audio output switch (active) failed")

    # === 2. peer 生成 + ICE gathering まで lock 外で進める ===
    pc = RTCPeerConnection()  # No ICE servers; LAN 直結 (Tailscale) 前提

    @pc.on("connectionstatechange")
    async def _on_state_change():
        logger.info("screen pc state=%s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            asyncio.create_task(_async_cleanup_if(pc))

    # decode=False で encoded packet を relay してるので _Yuv420Track の reformat は不要
    if player.video is not None:
        pc.addTrack(player.video)
    if player.audio is not None:
        pc.addTrack(player.audio)

    # try / finally で**必ず resource を片付ける** (CancelledError 含む).
    # `installed` が True で正常完了した時だけ globals 占有を維持し、
    # それ以外 (例外 / cancel) は player + ffmpeg + pc を確実に潰す。
    installed = False
    try:
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=typ))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await _wait_ice_gathering_complete(pc)
        except (Exception, asyncio.CancelledError) as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.exception("offer handling failed")
            raise

        # === 3. ここまで来たら全部できた → 短時間 lock を取って globals に install ===
        global _abr_task, _abr_target_kbps
        async with _lock:
            await _teardown()  # 旧 peer / player / ffmpeg を片付け
            _pc = pc
            _player = player
            _ffmpeg = proc
            installed = True

            # 診断: negotiated codec を SDP answer から拾って log
            try:
                ans_sdp = pc.localDescription.sdp
                for line in ans_sdp.splitlines():
                    if line.startswith("a=rtpmap:") and ("/" in line):
                        logger.error("[SCREEN-DIAG] negotiated rtpmap: %s", line)
            except Exception:
                logger.exception("[SCREEN-DIAG] codec log failed")

            # 初期 bitrate を ABR の target と一致させる
            _abr_target_kbps = int(SCREEN_SHARE.get("initial_bitrate_kbps", 1000))
            _set_video_max_bitrate(_abr_target_kbps)

            # ABR loop 起動 (既に動いてれば残す)
            if _abr_task is None or _abr_task.done():
                _abr_task = asyncio.create_task(_abr_loop())

        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    finally:
        if not installed:
            # globals に install してない = 自分専有のリソースなので潰す
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
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass


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


@router.get("/screen/stats")
async def screen_stats():
    """RTCPeerConnection の getStats() を JSON で返す診断エンドポイント。

    PWA 側から数秒ごとに poll して overlay 表示する用途。
    bitrate / fps の rate 値は frontend で時系列差分から計算する想定で、
    backend は raw counter (bytesSent, packetsSent, packetsLost 等) を出す。
    """
    if _pc is None:
        return {"connected": False}
    try:
        report = await _pc.getStats()
    except Exception:
        logger.exception("getStats failed")
        return {"connected": False, "error": "stats failed"}

    out = {
        "connected": _pc.connectionState == "connected",
        "state": _pc.connectionState,
        # iceConnectionState は connectionState と別軸 (failed / disconnected /
        # checking / connected)。 frontend の診断 overlay で接続不調の切り分けに使う。
        "iceConnectionState": getattr(_pc, "iceConnectionState", None),
        "ts": _time.time(),
        "abr_target_kbps": _abr_target_kbps,
        "codec": _video_codec_label,
        "video_out": {},
        "audio_out": {},
        "video_remote": {},
        "audio_remote": {},
        "network": {},
    }
    # 採択された candidate pair 経路 (host / srflx / relay) を抽出。
    # nominated=True のペアが「実際に使われてる経路」。 LAN 直結なら host、
    # NAT 越えなら srflx、 TURN 経由なら relay。 Tailscale だと通常 host。
    nominated_local_id = None
    nominated_remote_id = None
    candidate_types: dict[str, str] = {}
    for s in report.values():
        t = getattr(s, "type", None)
        if t == "candidate-pair" and getattr(s, "nominated", False):
            nominated_local_id = getattr(s, "localCandidateId", None)
            nominated_remote_id = getattr(s, "remoteCandidateId", None)
        elif t in ("local-candidate", "remote-candidate"):
            cid = getattr(s, "id", None)
            ctype = getattr(s, "candidateType", None)
            if cid and ctype:
                candidate_types[cid] = ctype
    if nominated_local_id and nominated_local_id in candidate_types:
        local_t = candidate_types[nominated_local_id]
        remote_t = candidate_types.get(nominated_remote_id, local_t)
        # 表示用: 両端が同じならその一語、 違えば矢印で。 多くの場合一致するので簡潔に。
        out["candidate_type"] = local_t if local_t == remote_t else f"{local_t}→{remote_t}"
    for s in report.values():
        t = getattr(s, "type", None)
        if t == "outbound-rtp":
            kind = getattr(s, "kind", None)
            slot = "video_out" if kind == "video" else ("audio_out" if kind == "audio" else None)
            if slot is None:
                continue
            entry = {
                "packetsSent": getattr(s, "packetsSent", 0),
                "bytesSent": getattr(s, "bytesSent", 0),
            }
            # 動画: framesEncoded を取れれば実 fps 算出に使う (aiortc 1.14+)
            if kind == "video":
                fe = getattr(s, "framesEncoded", None)
                if fe is not None:
                    entry["framesEncoded"] = fe
            out[slot] = entry
        elif t == "remote-inbound-rtp":
            kind = getattr(s, "kind", None)
            slot = "video_remote" if kind == "video" else ("audio_remote" if kind == "audio" else None)
            if slot is None:
                continue
            out[slot] = {
                "packetsLost": getattr(s, "packetsLost", 0),
                "jitter": float(getattr(s, "jitter", 0) or 0),
                "roundTripTime": float(getattr(s, "roundTripTime", 0) or 0),
            }
        elif t == "transport":
            out["network"]["bytesSent"] = getattr(s, "bytesSent", 0)
            out["network"]["bytesReceived"] = getattr(s, "bytesReceived", 0)
    return out
