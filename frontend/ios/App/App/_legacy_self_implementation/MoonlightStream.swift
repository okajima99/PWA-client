// MoonlightStream.swift
//
// moonlight-common-c の LiStartConnection を呼んで video/audio stream を受信する。
// C function pointers (@convention(c)) で callbacks を渡し、 global singleton 経由で
// VideoRenderer / AudioPlayer に forward する。
//
// 流れ:
//   1. /launch で rtsp://host:port を取得 (NvHTTPClient 経由)
//   2. SERVER_INFORMATION + STREAM_CONFIGURATION を組み立てる
//   3. 4 種 callbacks をセットアップ (decoder/audio/connection)
//   4. LiStartConnection() 呼び出し → 内部で RTSP/control/video/audio socket 立てて
//      callback で frame を流してくる
//
// 注意: @convention(c) callback は Swift の capture context を持てない。 故に
// MoonlightStream.shared (singleton) から VideoRenderer / AudioPlayer に転送する。

import Foundation

public final class MoonlightStream {
    public static let shared = MoonlightStream()

    public weak var videoRenderer: VideoRenderer?
    public weak var audioPlayer: AudioPlayer?

    /// state changes 通知 callback
    public var onState: ((String) -> Void)?

    private(set) public var isRunning = false
    private var streamConfig = STREAM_CONFIGURATION()
    private var serverInfo = SERVER_INFORMATION()

    // CSrtspSessionUrl 等の C string メモリを保持 (Swift String の lifetime に依存しないように)
    private var rtspUrlCString: UnsafeMutablePointer<CChar>?
    private var hostCString: UnsafeMutablePointer<CChar>?
    private var appVersionCString: UnsafeMutablePointer<CChar>?
    private var gfeVersionCString: UnsafeMutablePointer<CChar>?

    private init() {}

    /// 接続開始。 launch から取得した rtspUrl と pairing 情報を入れる。
    public func start(
        host: String,
        rtspUrl: String,
        appVersion: String,
        gfeVersion: String,
        serverCodecModeSupport: Int32,
        rikey: Data,
        rikeyId: UInt32,
        width: Int32 = 1920,
        height: Int32 = 1080,
        fps: Int32 = 60,
        bitrate: Int32 = 30_000
    ) -> Int32 {
        // 既存があれば停止
        if isRunning { stop() }

        // STREAM_CONFIGURATION
        LiInitializeStreamConfiguration(&streamConfig)
        streamConfig.width = width
        streamConfig.height = height
        streamConfig.fps = fps
        streamConfig.bitrate = bitrate
        streamConfig.packetSize = 1392
        streamConfig.streamingRemotely = STREAM_CFG_AUTO
        // AUDIO_CONFIGURATION_STEREO = MAKE_AUDIO_CONFIGURATION(2, 0x3) = (0x3 << 16) | (2 << 8) | 0xCA
        streamConfig.audioConfiguration = (0x3 << 16) | (2 << 8) | 0xCA
        streamConfig.supportedVideoFormats = VIDEO_FORMAT_H264 | VIDEO_FORMAT_H265
        streamConfig.colorSpace = COLORSPACE_REC_709
        streamConfig.colorRange = COLOR_RANGE_LIMITED
        streamConfig.encryptionFlags = Int32(ENCFLG_AUDIO)
        // remoteInputAesKey (16 bytes) + remoteInputAesIv (16 bytes、 rikeyId を big-endian で埋める)
        rikey.withUnsafeBytes { src in
            withUnsafeMutableBytes(of: &streamConfig.remoteInputAesKey) { dst in
                if let s = src.baseAddress, let d = dst.baseAddress {
                    let n = min(16, src.count, dst.count)
                    memcpy(d, s, n)
                }
            }
        }
        // IV: rikeyId を 4 bytes big-endian にして 16 bytes の先頭に
        var idBE = rikeyId.bigEndian
        withUnsafeMutableBytes(of: &streamConfig.remoteInputAesIv) { dst in
            withUnsafeBytes(of: &idBE) { src in
                if let s = src.baseAddress, let d = dst.baseAddress {
                    memset(d, 0, 16)
                    memcpy(d, s, min(4, src.count))
                }
            }
        }

        // SERVER_INFORMATION
        LiInitializeServerInformation(&serverInfo)
        hostCString = strdup(host)
        rtspUrlCString = strdup(rtspUrl)
        appVersionCString = strdup(appVersion)
        gfeVersionCString = strdup(gfeVersion)
        serverInfo.address = UnsafePointer(hostCString)
        serverInfo.rtspSessionUrl = UnsafePointer(rtspUrlCString)
        serverInfo.serverInfoAppVersion = UnsafePointer(appVersionCString)
        serverInfo.serverInfoGfeVersion = UnsafePointer(gfeVersionCString)
        serverInfo.serverCodecModeSupport = serverCodecModeSupport

        // Decoder callbacks
        var drCallbacks = DECODER_RENDERER_CALLBACKS()
        LiInitializeVideoCallbacks(&drCallbacks)
        drCallbacks.setup = mlDrSetup
        drCallbacks.start = mlDrStart
        drCallbacks.stop = mlDrStop
        drCallbacks.cleanup = mlDrCleanup
        drCallbacks.submitDecodeUnit = mlDrSubmitDecodeUnit

        // Audio callbacks (現状は Opus decode 未実装、 stub のみ)
        var arCallbacks = AUDIO_RENDERER_CALLBACKS()
        LiInitializeAudioCallbacks(&arCallbacks)
        arCallbacks.`init` = mlArInit
        arCallbacks.start = mlArStart
        arCallbacks.stop = mlArStop
        arCallbacks.cleanup = mlArCleanup
        arCallbacks.decodeAndPlaySample = mlArDecodeAndPlaySample

        // Connection listener callbacks
        var clCallbacks = CONNECTION_LISTENER_CALLBACKS()
        LiInitializeConnectionCallbacks(&clCallbacks)
        clCallbacks.stageStarting = mlClStageStarting
        clCallbacks.stageComplete = mlClStageComplete
        clCallbacks.stageFailed = mlClStageFailed
        clCallbacks.connectionStarted = mlClConnectionStarted
        clCallbacks.connectionTerminated = mlClConnectionTerminated
        // logMessage は variadic (C の `format, ...`) で Swift から bridge できないので skip
        clCallbacks.connectionStatusUpdate = mlClConnectionStatusUpdate

        let result = LiStartConnection(
            &serverInfo,
            &streamConfig,
            &clCallbacks,
            &drCallbacks,
            &arCallbacks,
            nil, 0,
            nil, 0
        )
        isRunning = (result == 0)
        return result
    }

    public func stop() {
        if isRunning {
            LiStopConnection()
            isRunning = false
        }
        // C string メモリ解放
        if let p = rtspUrlCString { free(p); rtspUrlCString = nil }
        if let p = hostCString { free(p); hostCString = nil }
        if let p = appVersionCString { free(p); appVersionCString = nil }
        if let p = gfeVersionCString { free(p); gfeVersionCString = nil }
    }

    // MARK: - Frame forwarding (called from C callbacks via singleton)

    fileprivate func forwardVideoFrame(_ data: UnsafePointer<UInt8>, length: Int, frameType: Int, presentationTimeUs: UInt64) {
        videoRenderer?.submitNAL(data: data, length: length, frameType: frameType, ptsUs: presentationTimeUs)
    }

    fileprivate func forwardAudioFrame(_ data: UnsafePointer<UInt8>, length: Int) {
        audioPlayer?.submitOpus(data: data, length: length)
    }

    fileprivate func notifyState(_ message: String) {
        DispatchQueue.main.async { [weak self] in
            self?.onState?(message)
        }
    }
}

// MARK: - C-compatible callback functions (top-level、 capture 不可)
// それぞれ MoonlightStream.shared にforwardする

private func mlDrSetup(videoFormat: Int32, width: Int32, height: Int32, redrawRate: Int32, context: UnsafeMutableRawPointer?, drFlags: Int32) -> Int32 {
    NSLog("[Moonlight] DR setup format=\(videoFormat) \(width)x\(height)@\(redrawRate)")
    return 0
}

private func mlDrStart() {
    NSLog("[Moonlight] DR start")
}

private func mlDrStop() {
    NSLog("[Moonlight] DR stop")
}

private func mlDrCleanup() {
    NSLog("[Moonlight] DR cleanup")
}

private func mlDrSubmitDecodeUnit(decodeUnit: UnsafeMutablePointer<DECODE_UNIT>?) -> Int32 {
    guard let du = decodeUnit?.pointee else { return DR_OK }
    // bufferList を辿って 1 frame 分の bytes を集める
    var frame = Data()
    var entry: UnsafeMutablePointer<LENTRY>? = du.bufferList
    while let e = entry {
        let len = Int(e.pointee.length)
        if let raw = e.pointee.data {
            raw.withMemoryRebound(to: UInt8.self, capacity: len) { p in
                frame.append(p, count: len)
            }
        }
        entry = e.pointee.next
    }
    frame.withUnsafeBytes { buf in
        if let p = buf.bindMemory(to: UInt8.self).baseAddress {
            MoonlightStream.shared.forwardVideoFrame(
                p, length: frame.count,
                frameType: Int(du.frameType),
                presentationTimeUs: du.presentationTimeUs
            )
        }
    }
    return DR_OK
}

private func mlArInit(audioConfiguration: Int32, opusConfig: UnsafeMutablePointer<OPUS_MULTISTREAM_CONFIGURATION>?, context: UnsafeMutableRawPointer?, arFlags: Int32) -> Int32 {
    NSLog("[Moonlight] AR init audioConfig=\(audioConfiguration)")
    return 0
}

private func mlArStart() {
    NSLog("[Moonlight] AR start")
}

private func mlArStop() {
    NSLog("[Moonlight] AR stop")
}

private func mlArCleanup() {
    NSLog("[Moonlight] AR cleanup")
}

private func mlArDecodeAndPlaySample(sampleData: UnsafeMutablePointer<CChar>?, sampleLength: Int32) {
    guard let p = sampleData, sampleLength > 0 else { return }
    p.withMemoryRebound(to: UInt8.self, capacity: Int(sampleLength)) { up in
        MoonlightStream.shared.forwardAudioFrame(up, length: Int(sampleLength))
    }
}

private func mlClStageStarting(stage: Int32) {
    NSLog("[Moonlight] stage starting: \(stage)")
    MoonlightStream.shared.notifyState("stage starting \(stage)")
}

private func mlClStageComplete(stage: Int32) {
    NSLog("[Moonlight] stage complete: \(stage)")
}

private func mlClStageFailed(stage: Int32, errorCode: Int32) {
    NSLog("[Moonlight] stage failed: \(stage) err=\(errorCode)")
    MoonlightStream.shared.notifyState("stage failed \(stage) err=\(errorCode)")
}

private func mlClConnectionStarted() {
    NSLog("[Moonlight] connection started")
    MoonlightStream.shared.notifyState("connected")
}

private func mlClConnectionTerminated(errorCode: Int32) {
    NSLog("[Moonlight] connection terminated err=\(errorCode)")
    MoonlightStream.shared.notifyState("terminated err=\(errorCode)")
}

private func mlClConnectionStatusUpdate(connectionStatus: Int32) {
    NSLog("[Moonlight] conn status update: \(connectionStatus)")
}
