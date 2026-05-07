// MoonlightBridge.swift
//
// build 26 で「web 主導アーキテクチャ」 化。 native は generic API のみ提供:
//   - request(path, params, useTLS) → 生 HTTP に client cert 付与して XML を返す
//   - startStream(config) → JS から渡された設定値で LiStartConnection を起動
//   - pair(host, pin) → SecIdentity 周りは native でしかできないのでここに残す
//   - disconnect()
//
// 接続フロー (serverinfo → cancel → applist → launch → startStream) は
// frontend/src/native/moonlight-flow.js が orchestrate。 値計算 (audioConfig 等) も JS。
//
// stage 進行は ConnectionCallbacks 経由で Swift 側に来る → notifyStatus で JS に push。

import Foundation
import UIKit
import AVKit
import AVFoundation
import CoreMedia

@objc public final class MoonlightBridge: NSObject {

    public typealias StatusCallback = (String, [String: Any]) -> Void

    private var statusCallback: StatusCallback?

    /// stream を表示する UIView。 Capacitor の view 階層に貼り付けてから渡す。
    public weak var streamView: UIView?

    /// host のみ覚えておく (= request / startStream で再利用、 JS から都度 host 渡されるので必須ではない)
    private var currentHost: String?

    private var pairingManager: PairingManager?
    private var nvHttp: NvHTTPClient?
    private var renderer: VideoDecoderRenderer?
    private var connection: Connection?
    private let connectionQueue = OperationQueue()

    // MARK: - Public API

    public func setStatusCallback(_ cb: @escaping StatusCallback) {
        self.statusCallback = cb
    }

    /// Sunshine と PIN ペアリング (動作確認済の Swift 実装を維持)。
    /// SecIdentity 操作が要るので native のまま。
    public func pair(host: String, pin: String, completion: @escaping (Result<Void, Error>) -> Void) {
        notifyStatus(event: "pairing", payload: ["host": host])
        let pm = PairingManager(host: host, deviceName: "App")
        self.pairingManager = pm
        self.currentHost = host
        pm.pair(pin: pin) { [weak self] result in
            switch result {
            case .success:
                self?.notifyStatus(event: "paired", payload: [:])
                completion(.success(()))
            case .failure(let err):
                self?.notifyStatus(event: "error", payload: ["message": "pair failed: \(err)"])
                completion(.failure(err))
            }
        }
    }

    /// 生 HTTP (client cert 付き) を JS から呼ぶ。 XML 文字列で返す。
    /// host は pair() で記憶した値、 もしくは引数で上書き。
    public func request(host: String?, path: String, params: [String: String], useTLS: Bool, completion: @escaping (Result<String, Error>) -> Void) {
        let targetHost = host ?? currentHost
        guard let h = targetHost else {
            completion(.failure(NSError(domain: "MoonlightBridge", code: -10, userInfo: [NSLocalizedDescriptionKey: "host 未設定 (pair 済の host を再利用するか引数で指定)"])))
            return
        }
        // pairing 完了済の cert を NvHTTPClient に inject
        let pm = pairingManager ?? PairingManager(host: h, deviceName: "App")
        self.pairingManager = pm
        do { _ = try pm.loadClientIdentity() } catch {
            // pair してない時の serverinfo は cert 不要、 そのまま続行
        }
        let http = pm.http
        self.nvHttp = http
        self.currentHost = h

        http.get(path: path, params: params, useTLS: useTLS) { result in
            switch result {
            case .success(let data):
                let body = String(data: data, encoding: .utf8) ?? ""
                completion(.success(body))
            case .failure(let e):
                completion(.failure(e))
            }
        }
    }

    /// Stream 開始: JS が serverinfo / cancel / applist / launch を済ませた後、
    /// 抽出済の値を渡してくる → そのまま LiStartConnection を呼ぶ。
    public func startStream(config: [String: Any], completion: @escaping (Result<Void, Error>) -> Void) {
        guard let view = streamView else {
            completion(.failure(NSError(domain: "MoonlightBridge", code: -1,
                                        userInfo: [NSLocalizedDescriptionKey: "streamView 未設定"])))
            return
        }
        // 必須キーの取り出し (型は配慮: Capacitor JS から数値は NSNumber で来る)
        guard
            let host = config["host"] as? String,
            let appVersion = config["appVersion"] as? String,
            let rtspSessionUrl = config["rtspSessionUrl"] as? String,
            let riKeyHex = config["riKeyHex"] as? String,
            let riKeyIdNum = config["riKeyId"] as? NSNumber,
            let width = config["width"] as? NSNumber,
            let height = config["height"] as? NSNumber,
            let fps = config["fps"] as? NSNumber,
            let bitrate = config["bitrate"] as? NSNumber,
            let audioConfig = config["audioConfig"] as? NSNumber,
            let supportedVideoFormats = config["supportedVideoFormats"] as? NSNumber
        else {
            completion(.failure(NSError(domain: "MoonlightBridge", code: -3,
                                        userInfo: [NSLocalizedDescriptionKey: "config に必須キーが欠けてる"])))
            return
        }
        let gfeVersion = (config["gfeVersion"] as? String) ?? ""
        let codecModeSupport = (config["serverCodecModeSupport"] as? NSNumber)?.int32Value ?? 0
        let useFramePacing = (config["useFramePacing"] as? NSNumber)?.boolValue ?? true

        guard let riKey = Self.dataFromHex(riKeyHex) else {
            completion(.failure(NSError(domain: "MoonlightBridge", code: -4,
                                        userInfo: [NSLocalizedDescriptionKey: "riKeyHex が hex 文字列でない"])))
            return
        }

        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }

            let cfg = StreamConfiguration()
            cfg.host = host
            cfg.appVersion = appVersion
            cfg.gfeVersion = gfeVersion
            cfg.rtspSessionUrl = rtspSessionUrl
            cfg.serverCodecModeSupport = codecModeSupport
            cfg.width = width.int32Value
            cfg.height = height.int32Value
            cfg.frameRate = fps.int32Value
            cfg.bitRate = bitrate.int32Value
            cfg.riKey = riKey
            cfg.riKeyId = riKeyIdNum.int32Value
            cfg.audioConfiguration = audioConfig.int32Value
            cfg.supportedVideoFormats = supportedVideoFormats.int32Value
            cfg.useFramePacing = useFramePacing

            DebugLog.send("MoonlightBridge", "startStream config: width=\(cfg.width) height=\(cfg.height) fps=\(cfg.frameRate) bitrate=\(cfg.bitRate) audioCfg=0x\(String(cfg.audioConfiguration, radix: 16)) videoFmts=0x\(String(cfg.supportedVideoFormats, radix: 16))")

            let aspectRatio = Float(cfg.width) / Float(cfg.height)
            let renderer = VideoDecoderRenderer(
                view: view,
                callbacks: self,
                streamAspectRatio: aspectRatio,
                useFramePacing: useFramePacing
            )
            self.renderer = renderer

            guard let conn = Connection(config: cfg, renderer: renderer, connectionCallbacks: self) else {
                completion(.failure(NSError(domain: "MoonlightBridge", code: -5,
                                            userInfo: [NSLocalizedDescriptionKey: "Connection init returned nil"])))
                return
            }
            self.connection = conn
            self.connectionQueue.addOperation(conn)
            completion(.success(()))
        }
    }

    public func disconnect() {
        teardownPiP()
        connection?.terminate()
        connection = nil
        renderer?.stop()
        renderer = nil
    }

    // MARK: - 統計情報取得 (= 遅延 overlay 用、 リアルタイム fps/RTT/kbps/codec)

    public func getStats() -> [String: Any] {
        var stats: [String: Any] = ["connected": connection != nil]
        var rtt: UInt32 = 0
        var variance: UInt32 = 0
        if LiGetEstimatedRttInfo(&rtt, &variance) {
            stats["rttMs"] = Int(rtt)
            stats["rttVarianceMs"] = Int(variance)
        }
        if let conn = connection {
            var v = video_stats_t()
            if conn.getVideoStats(&v) {
                let elapsed = v.endTime - v.startTime
                let fps = elapsed > 0 ? Double(v.receivedFrames) / elapsed : 0
                stats["fps"] = fps
                stats["totalFrames"] = Int(v.totalFrames)
                stats["receivedFrames"] = Int(v.receivedFrames)
                stats["droppedFrames"] = Int(v.networkDroppedFrames)
                if v.framesWithHostProcessingLatency > 0 {
                    stats["hostProcessingLatencyMs"] = Double(v.totalHostProcessingLatency) / Double(v.framesWithHostProcessingLatency) / 10.0
                }
            }
            if let codec = conn.getActiveCodecName() { stats["codec"] = codec }
        }
        return stats
    }

    // MARK: - 追加入力 method (= IME / 高解像度スクロール / 等)

    /// IME / Unicode テキスト送信 (= 日本語など多バイト文字を直接 Mac に)
    public func sendUtf8Text(_ text: String) {
        text.withCString { cstr in
            LiSendUtf8TextEvent(cstr, UInt32(strlen(cstr)))
        }
    }

    /// 高解像度スクロール (= magic mouse / trackpad、 1/120 単位)
    public func sendHighResScroll(delta: Int16) { LiSendHighResScrollEvent(delta) }
    public func sendHighResHScroll(delta: Int16) { LiSendHighResHScrollEvent(delta) }

    /// IDR frame 再要求 (= PiP 復帰 / network glitch 後の画像復活)
    public func requestIdrFrame() { LiRequestIdrFrame() }

    /// 接続中断 (= LiInterruptConnection、 disconnect の強い版)
    public func interrupt() { LiInterruptConnection() }

    // MARK: - Phase 5 PiP (build 27 で追加)

    private var pipController: AVPictureInPictureController?
    private var pipDelegate: PiPDelegate?

    /// AVSampleBufferDisplayLayer を PiP 化。 stream 開始後に呼ぶ前提。
    public func enablePiP() -> Bool {
        guard AVPictureInPictureController.isPictureInPictureSupported() else { return false }
        // 公式 VideoDecoderRenderer は内部で AVSampleBufferDisplayLayer を保持してる。
        // 実装公開されてないので KVC で取得する (= "displayLayer" property)。
        guard let r = renderer,
              let layer = r.value(forKey: "displayLayer") as? AVSampleBufferDisplayLayer else {
            return false
        }
        if pipController == nil {
            let source = AVPictureInPictureController.ContentSource(sampleBufferDisplayLayer: layer, playbackDelegate: self)
            let ctrl = AVPictureInPictureController(contentSource: source)
            ctrl.canStartPictureInPictureAutomaticallyFromInline = true
            let delegate = PiPDelegate { [weak self] event in
                self?.notifyStatus(event: "pip", payload: ["state": event])
            }
            ctrl.delegate = delegate
            self.pipController = ctrl
            self.pipDelegate = delegate
        }
        DispatchQueue.main.async {
            self.pipController?.startPictureInPicture()
        }
        return true
    }

    public func disablePiP() {
        DispatchQueue.main.async { [weak self] in
            self?.pipController?.stopPictureInPicture()
        }
    }

    private func teardownPiP() {
        pipController?.stopPictureInPicture()
        pipController = nil
        pipDelegate = nil
    }

    // MARK: - Phase 5.5 入力 (build 27 で追加: moonlight-common-c の LiSend* 全部 expose)

    /// 相対マウス移動 (= trackpad 風)
    public func sendMouseMove(dx: Int16, dy: Int16) {
        LiSendMouseMoveEvent(dx, dy)
    }

    /// 絶対マウス位置 (= 画面の相対座標、 refW/refH の plane 上の座標として)
    public func sendMousePosition(x: Int16, y: Int16, refW: Int16, refH: Int16) {
        LiSendMousePositionEvent(x, y, refW, refH)
    }

    /// マウスボタン: button = 1 (left) / 2 (middle) / 3 (right) / 4-5 (extra)
    /// action: 0x07 = press, 0x08 = release
    public func sendMouseButton(button: UInt8, action: UInt8) {
        LiSendMouseButtonEvent(CChar(bitPattern: action), Int32(button))
    }

    /// スクロール (vertical、 scrollClicks 単位、 1 click = 1 ノッチ)
    public func sendScroll(delta: Int8) {
        LiSendScrollEvent(delta)
    }

    /// 横スクロール
    public func sendHScroll(delta: Int8) {
        LiSendHScrollEvent(delta)
    }

    /// キーボードイベント
    /// keyCode = HID scancode (Windows VK と互換)
    /// modifiers = bitmask: 0x01 Shift, 0x02 Ctrl, 0x04 Alt, 0x08 Meta(Cmd/Win)
    /// action = 0x03 (down) / 0x04 (up)
    public func sendKey(keyCode: Int16, modifiers: UInt8, action: UInt8) {
        LiSendKeyboardEvent(keyCode, CChar(bitPattern: action), CChar(bitPattern: modifiers))
    }

    /// マルチタッチイベント (= iPhone screen をそのまま Mac の touch input に転送)
    /// eventType: 0=down, 1=up, 2=move, 3=cancel
    public func sendTouch(eventType: UInt8, pointerId: UInt32, x: Float, y: Float, pressure: Float) {
        LiSendTouchEvent(eventType, pointerId, x, y, pressure, 0, 0, UInt16(LI_ROT_UNKNOWN))
    }

    // MARK: - Phase 6 一部 (build 27 で先回り)

    /// Haptic feedback (= 操作のフィードバック)
    /// pattern: "light" / "medium" / "heavy" / "selection" / "success" / "warning" / "error"
    public func haptic(pattern: String) {
        DispatchQueue.main.async {
            switch pattern {
            case "light":
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            case "medium":
                UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            case "heavy":
                UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
            case "selection":
                UISelectionFeedbackGenerator().selectionChanged()
            case "success":
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            case "warning":
                UINotificationFeedbackGenerator().notificationOccurred(.warning)
            case "error":
                UINotificationFeedbackGenerator().notificationOccurred(.error)
            default:
                break
            }
        }
    }

    // Face ID 起動ロックは iOS 16+ の標準機能 (= ホーム画面 App 長押し → "Face ID でロック")
    // で済むので自前 authenticate は実装しない。

    // MARK: - Private

    private func notifyStatus(event: String, payload: [String: Any]) {
        DispatchQueue.main.async { [weak self] in
            self?.statusCallback?(event, payload)
        }
    }

    private static func dataFromHex(_ hex: String) -> Data? {
        guard hex.count % 2 == 0 else { return nil }
        var data = Data()
        var idx = hex.startIndex
        while idx < hex.endIndex {
            let next = hex.index(idx, offsetBy: 2)
            guard let b = UInt8(hex[idx..<next], radix: 16) else { return nil }
            data.append(b)
            idx = next
        }
        return data
    }
}

// MARK: - ConnectionCallbacks (公式 Connection からの通知を Swift 側で受ける)

extension MoonlightBridge: ConnectionCallbacks {

    public func connectionStarted() {
        DebugLog.send("MoonlightBridge", "connectionStarted")
        notifyStatus(event: "connectionStarted", payload: [:])
    }

    public func connectionTerminated(_ errorCode: Int32) {
        DebugLog.send("MoonlightBridge", "connectionTerminated code=\(errorCode)")
        notifyStatus(event: "connectionTerminated", payload: ["code": Int(errorCode)])
    }

    public func stageStarting(_ stageName: UnsafePointer<CChar>!) {
        let n = String(cString: stageName)
        DebugLog.send("MoonlightBridge", "stageStarting: \(n)")
        notifyStatus(event: "stageStarting", payload: ["name": n])
    }

    public func stageComplete(_ stageName: UnsafePointer<CChar>!) {
        let n = String(cString: stageName)
        DebugLog.send("MoonlightBridge", "stageComplete: \(n)")
        notifyStatus(event: "stageComplete", payload: ["name": n])
    }

    public func stageFailed(_ stageName: UnsafePointer<CChar>!, withError errorCode: Int32, portTestFlags: Int32) {
        let n = String(cString: stageName)
        DebugLog.send("MoonlightBridge", "stageFailed: \(n) code=\(errorCode) flags=\(portTestFlags)")
        notifyStatus(event: "stageFailed", payload: ["name": n, "code": Int(errorCode), "portTestFlags": Int(portTestFlags)])
    }

    public func launchFailed(_ message: String!) {
        DebugLog.send("MoonlightBridge", "launchFailed: \(message ?? "")")
        notifyStatus(event: "launchFailed", payload: ["message": message ?? ""])
    }

    public func rumble(_ controllerNumber: UInt16, lowFreqMotor: UInt16, highFreqMotor: UInt16) {}

    public func connectionStatusUpdate(_ status: Int32) {
        DebugLog.send("MoonlightBridge", "connectionStatusUpdate status=\(status)")
        notifyStatus(event: "connectionStatusUpdate", payload: ["status": Int(status)])
    }

    public func setHdrMode(_ enabled: Bool) {
        DebugLog.send("MoonlightBridge", "setHdrMode \(enabled)")
    }

    public func rumbleTriggers(_ controllerNumber: UInt16, leftTrigger: UInt16, rightTrigger: UInt16) {}

    public func setMotionEventState(_ controllerNumber: UInt16, motionType: UInt8, reportRateHz: UInt16) {}

    public func setControllerLed(_ controllerNumber: UInt16, r: UInt8, g: UInt8, b: UInt8) {}

    public func videoContentShown() {
        DebugLog.send("MoonlightBridge", "videoContentShown (= IDR frame received + display visible)")
        notifyStatus(event: "videoContentShown", payload: [:])
    }
}

// MARK: - PiP delegate (= state 通知のためだけの薄い wrapper)

private final class PiPDelegate: NSObject, AVPictureInPictureControllerDelegate {
    let onEvent: (String) -> Void
    init(onEvent: @escaping (String) -> Void) { self.onEvent = onEvent }

    func pictureInPictureControllerWillStartPictureInPicture(_ c: AVPictureInPictureController) { onEvent("willStart") }
    func pictureInPictureControllerDidStartPictureInPicture(_ c: AVPictureInPictureController) { onEvent("didStart") }
    func pictureInPictureControllerWillStopPictureInPicture(_ c: AVPictureInPictureController) { onEvent("willStop") }
    func pictureInPictureControllerDidStopPictureInPicture(_ c: AVPictureInPictureController) { onEvent("didStop") }
    func pictureInPictureController(_ c: AVPictureInPictureController, failedToStartPictureInPictureWithError error: Error) { onEvent("failed:\(error.localizedDescription)") }
}

// MARK: - PiP playback delegate (= sample buffer 用、 必要 method を最小実装)

extension MoonlightBridge: AVPictureInPictureSampleBufferPlaybackDelegate {
    public func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, setPlaying playing: Bool) {
        // stream は常に再生中扱い、 何もしない
    }

    public func pictureInPictureControllerTimeRangeForPlayback(_ pictureInPictureController: AVPictureInPictureController) -> CMTimeRange {
        // live stream なので無限 range
        return CMTimeRange(start: .negativeInfinity, duration: .positiveInfinity)
    }

    public func pictureInPictureControllerIsPlaybackPaused(_ pictureInPictureController: AVPictureInPictureController) -> Bool {
        return false
    }

    public func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, didTransitionToRenderSize newRenderSize: CMVideoDimensions) {
        // PiP window size 変化、 renderer 側は AVSampleBufferDisplayLayer の videoGravity でフィット
    }

    public func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, skipByInterval skipInterval: CMTime, completion completionHandler: @escaping () -> Void) {
        completionHandler()
    }
}
