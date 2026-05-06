// MoonlightBridge.swift
//
// 高レベル API: Sunshine ホストとの pair / connect / disconnect を担当する。
// 内部で PairingManager (4 段 handshake) と moonlight-common-c (LiStartConnection)
// を呼び出す。
//
// 現状: pair まで実装、 connect/stream は Phase 3 後半で実装予定。

import Foundation

@objc public final class MoonlightBridge: NSObject {

    @objc public enum BridgeState: Int {
        case idle = 0
        case pairing = 1
        case connecting = 2
        case connected = 3
        case failed = 4
    }

    public typealias StatusCallback = (BridgeState, String?) -> Void

    private(set) public var state: BridgeState = .idle
    private var statusCallback: StatusCallback?

    public var videoRenderer: VideoRenderer?
    public var audioPlayer: AudioPlayer?

    private var pairingManager: PairingManager?

    // MARK: - Public API

    public func setStatusCallback(_ cb: @escaping StatusCallback) {
        self.statusCallback = cb
    }

    /// Sunshine と PIN ペアリング。 host: Tailscale ホスト名 / IP、 pin: Sunshine Web UI に
    /// "PIN" タブで生成した 4 桁。 成功すると以降の接続で再ペア不要 (cert は Keychain 保存済)。
    public func pair(host: String, pin: String, completion: @escaping (Result<Void, Error>) -> Void) {
        updateState(.pairing)
        let pm = PairingManager(host: host, deviceName: "App")
        self.pairingManager = pm
        pm.pair(pin: pin) { [weak self] result in
            switch result {
            case .success:
                self?.updateState(.idle)
                completion(.success(()))
            case .failure(let err):
                self?.updateState(.failed, error: String(describing: err))
                completion(.failure(err))
            }
        }
    }

    /// stream session 開始。 paired 済 host に /serverinfo で codec 取得 → /launch で
    /// rtsp URL 取得 → LiStartConnection で stream 開始。 callbacks 経由で video/audio 受信。
    public func connect(host: String, completion: @escaping (Result<Void, Error>) -> Void) {
        updateState(.connecting)
        let pm = self.pairingManager ?? PairingManager(host: host, deviceName: "App")
        // ペア済 cert を NvHTTPClient に inject
        do { _ = try pm.loadClientIdentity() } catch {
            updateState(.failed, error: "client cert load failed: \(error)")
            completion(.failure(error)); return
        }
        let http = pm.http

        http.fetchServerInfo { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let e):
                self.updateState(.failed, error: "serverinfo failed: \(e)")
                completion(.failure(e))
            case .success(let info):
                // rikey: 16 random bytes (hex)、 rikeyId: 4 byte (任意)
                let rikey = PairingManager.randomBytes(16)
                let rikeyHex = PairingManager.hexFromData(rikey)
                let rikeyId: UInt32 = UInt32.random(in: 1..<UInt32.max)
                // rikeyid は hex で送る (Sunshine 仕様、 decimal だと sessionUrl0 が返らない)
                let rikeyIdHex = String(format: "%08x", rikeyId)
                http.launch(
                    appId: "881448767", // Sunshine Desktop の App ID (固定値で OK)
                    mode: "1920x1080x60",
                    rikey: rikeyHex,
                    rikeyId: rikeyIdHex
                ) { [weak self] r in
                    guard let self = self else { return }
                    switch r {
                    case .failure(let e):
                        self.updateState(.failed, error: "launch failed: \(e)")
                        completion(.failure(e))
                    case .success(let rtspUrl):
                        // MoonlightStream に renderer / player を inject
                        MoonlightStream.shared.videoRenderer = self.videoRenderer
                        MoonlightStream.shared.audioPlayer = self.audioPlayer
                        MoonlightStream.shared.onState = { [weak self] msg in
                            self?.updateState(.connected, error: msg)
                        }
                        // LiStartConnection は同期 (= 成功で 0 返してから callback 経由で frame)
                        let res = MoonlightStream.shared.start(
                            host: host,
                            rtspUrl: rtspUrl,
                            appVersion: info.appVersion,
                            gfeVersion: info.gfeVersion,
                            serverCodecModeSupport: info.codecModeSupport,
                            rikey: rikey,
                            rikeyId: rikeyId
                        )
                        if res == 0 {
                            self.updateState(.connected)
                            completion(.success(()))
                        } else {
                            self.updateState(.failed, error: "LiStartConnection rc=\(res)")
                            completion(.failure(NSError(domain: "Moonlight", code: Int(res))))
                        }
                    }
                }
            }
        }
    }

    /// 切断。 LiStopConnection を呼んで stream を止める。
    public func disconnect() {
        MoonlightStream.shared.stop()
        videoRenderer?.flush()
        updateState(.idle)
    }

    /// stream 中の RTT を取得 (UI overlay 用)。 LiGetEstimatedRttInfo を呼ぶ。
    public func getEstimatedRtt() -> (rtt: UInt32, variance: UInt32)? {
        return nil
    }

    // MARK: - Private

    private func updateState(_ new: BridgeState, error: String? = nil) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            self.state = new
            self.statusCallback?(new, error)
        }
    }
}
