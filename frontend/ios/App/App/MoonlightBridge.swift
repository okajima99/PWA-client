// MoonlightBridge.swift
//
// 高レベル API: Sunshine ホストとの pair / connect / disconnect を担当する。
// 内部で moonlight-common-c (C library) を呼び出す。
//
// Phase 3 で実装する部分:
//   1. HTTPS pairing (GET /pair, POST /pair で AES + RSA 鍵交換)
//      → moonlight-common-c には pairing API は無い、 自前実装が要る
//      → 参考: moonlight-android の NvHTTP.java、 moonlight-qt の NvHTTP.cpp
//   2. /serverinfo / /applist / /launch / /resume の HTTPS API 呼び出し
//   3. LiStartConnection で stream 開始、 callbacks に VideoRenderer / AudioPlayer を hook
//   4. ペア済 cert / key の Keychain 保存
//
// 現状: skeleton のみ。 状態遷移とインターフェースだけ用意して、 実装は TODO。

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

    // VideoRenderer / AudioPlayer は Plugin から inject される
    public var videoRenderer: VideoRenderer?
    public var audioPlayer: AudioPlayer?

    // MARK: - Public API

    public func setStatusCallback(_ cb: @escaping StatusCallback) {
        self.statusCallback = cb
    }

    /// Sunshine ホストに接続。 初回は PIN ペアリング、 以降は保存済証明書で自動。
    public func connect(host: String, pin: String?, completion: @escaping (Result<Void, Error>) -> Void) {
        // TODO Phase 3:
        //   1. Keychain から既存 cert / key を取得、 無ければ pairing が必要
        //   2. GET https://<host>:47984/serverinfo で host 情報取得
        //   3. ペア済か確認、 未ペアなら HTTPS pair flow:
        //      - クライアント PIN salt 生成
        //      - GET /pair?devicename=...&phrase=getservercert で server 公開鍵取得
        //      - AES 鍵を PIN + salt から導出、 challenge / response で双方検証
        //      - 双方の cert を交換、 Keychain に保存
        //   4. ペア成立後 POST /launch?... で stream session 開始 (rikey/rikeyid 等を渡す)
        //   5. moonlight-common-c の LiInitializeStreamConfiguration / LiStartConnection を呼ぶ
        //   6. callbacks (decoder / audio / connection listener) を設定
        //
        //   現状ダミー: 0.5 秒待って success
        updateState(.connecting)
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.updateState(.connected)
            completion(.success(()))
        }
    }

    /// 切断。 LiStopConnection を呼んで stream を止める。
    public func disconnect() {
        // TODO Phase 3:
        //   - LiStopConnection() を呼ぶ
        //   - VideoRenderer / AudioPlayer の停止
        //   - keychain は保持 (次回再接続のため)
        updateState(.idle)
    }

    /// stream 中の RTT を取得 (UI overlay 用)。 LiGetEstimatedRttInfo を呼ぶ。
    public func getEstimatedRtt() -> (rtt: UInt32, variance: UInt32)? {
        // TODO Phase 3:
        //   var rtt: UInt32 = 0
        //   var variance: UInt32 = 0
        //   if LiGetEstimatedRttInfo(&rtt, &variance) {
        //     return (rtt, variance)
        //   }
        //   return nil
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
