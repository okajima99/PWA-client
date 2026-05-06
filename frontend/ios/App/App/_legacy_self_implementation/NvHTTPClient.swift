// NvHTTPClient.swift
//
// Sunshine ホストの HTTPS API を叩く client。 Moonlight protocol の HTTP 層。
//
// 主要 endpoint:
//   - GET  https://<host>:47984/serverinfo            (host metadata、 paired 状態確認)
//   - GET  https://<host>:47984/pair?...               (PIN ペアリングの 4 段 handshake)
//   - GET  https://<host>:47984/applist                (起動可能アプリ一覧)
//   - GET  https://<host>:47984/launch?...             (stream session 開始、 rikey/rikeyid 渡す)
//   - GET  https://<host>:47984/resume?...             (中断したセッション再開)
//   - POST https://<host>:47984/cancel                 (現セッション cancel)
//
// 自己署名証明書を Sunshine 側で使う仕様なので、 server cert は Trust on First Use
// (TOFU) ベースで Keychain に保存して以降固定する。 Apple の HTTPS pinning と同じ
// 概念だが、 pin 対象が動的に決まる点だけ違う。
//
// クライアント証明書 (= ペアリング後発行する自己署名 RSA-2048 cert) は
// PairingManager 側で生成 / Keychain 保存され、 ここでは URLSessionDelegate 経由で
// 提示する。

import Foundation
import Security

@objc public final class NvHTTPClient: NSObject {

    public enum NvHTTPError: Error {
        case invalidURL
        case httpStatus(Int)
        case malformedResponse(String)
        case missingField(String)
        case pairFailed(String)
        case noClientCertificate
    }

    public let host: String
    public let httpsPort: Int   // 47984 (TLS) for /pair, /serverinfo (paired)
    public let httpPort: Int    // 47989 (cleartext) for /serverinfo (initial probe)

    /// クライアント証明書 + 秘密鍵 (PairingManager から inject)。
    /// 未ペアリング状態では nil、 ペアリング完了後に設定される。
    public var clientIdentity: SecIdentity?

    /// 信頼する server cert SHA256 fingerprint (pinning)、 TOFU で初回保存。
    public var pinnedServerCertSHA256: Data?

    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        // Phase 2 (clientchallenge) は server がユーザの PIN 入力 (Sunshine Web UI) を
        // 最大 60 秒待つ仕様なので、 client は 90 秒 (60 + 余裕) timeout を許容する。
        cfg.timeoutIntervalForRequest = 90
        cfg.timeoutIntervalForResource = 120
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()

    public init(host: String, httpsPort: Int = 47984, httpPort: Int = 47989) {
        self.host = host
        self.httpsPort = httpsPort
        self.httpPort = httpPort
        super.init()
    }

    // MARK: - Public API

    /// HTTPS GET、 query 引数を URL に組み込む。
    public func get(
        path: String,
        params: [String: String] = [:],
        useTLS: Bool = true,
        completion: @escaping (Result<Data, Error>) -> Void
    ) {
        guard let url = buildURL(path: path, params: params, useTLS: useTLS) else {
            completion(.failure(NvHTTPError.invalidURL))
            return
        }
        let task = session.dataTask(with: url) { data, resp, err in
            if let err = err { completion(.failure(err)); return }
            guard let http = resp as? HTTPURLResponse else {
                completion(.failure(NvHTTPError.malformedResponse("no HTTPURLResponse")))
                return
            }
            if !(200...299).contains(http.statusCode) {
                completion(.failure(NvHTTPError.httpStatus(http.statusCode)))
                return
            }
            completion(.success(data ?? Data()))
        }
        task.resume()
    }

    // MARK: - URL building

    private func buildURL(path: String, params: [String: String], useTLS: Bool) -> URL? {
        var components = URLComponents()
        components.scheme = useTLS ? "https" : "http"
        components.host = host
        components.port = useTLS ? httpsPort : httpPort
        components.path = path.hasPrefix("/") ? path : "/\(path)"
        components.queryItems = params.map { URLQueryItem(name: $0.key, value: $0.value) }
        return components.url
    }

    // MARK: - Higher-level API

    /// /serverinfo (HTTPS、 paired client cert で識別) を叩いて 主要 field を抽出。
    public func fetchServerInfo(completion: @escaping (Result<(appVersion: String, gfeVersion: String, codecModeSupport: Int32, paired: Bool), Error>) -> Void) {
        get(path: "/serverinfo", useTLS: true) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let body):
                let appV = NvHTTPClient.extractXMLValue(data: body, tag: "appversion") ?? ""
                let gfeV = NvHTTPClient.extractXMLValue(data: body, tag: "GfeVersion") ?? ""
                let codecStr = NvHTTPClient.extractXMLValue(data: body, tag: "ServerCodecModeSupport") ?? "0"
                let pairedStr = NvHTTPClient.extractXMLValue(data: body, tag: "PairStatus") ?? "0"
                let codec = Int32(codecStr) ?? 0
                completion(.success((appV, gfeV, codec, pairedStr == "1")))
            }
        }
    }

    /// /launch (HTTPS) で stream session 開始。 rtsp URL を返す。
    public func launch(
        appId: String, mode: String, rikey: String, rikeyId: String,
        completion: @escaping (Result<String, Error>) -> Void
    ) {
        let params: [String: String] = [
            "uniqueid": "0123456789ABCDEF",
            "appid": appId,
            "mode": mode,
            "additionalStates": "1",
            "sops": "1",
            "rikey": rikey,
            "rikeyid": rikeyId,
            "localAudioPlayMode": "0",
            "surroundAudioInfo": "196610",
            "remoteControllersBitmap": "0",
            "gcmap": "0",
        ]
        get(path: "/launch", params: params, useTLS: true) { result in
            switch result {
            case .failure(let e): completion(.failure(e))
            case .success(let body):
                guard let url = NvHTTPClient.extractXMLValue(data: body, tag: "sessionUrl0") else {
                    completion(.failure(NvHTTPError.malformedResponse("sessionUrl0 missing")))
                    return
                }
                completion(.success(url))
            }
        }
    }

    // MARK: - Helpers

    /// Moonlight protocol の応答 XML から特定タグを抽出。
    /// XML は単純な <root><foo>val</foo></root> 構造なので正規表現で十分。
    public static func extractXMLValue(data: Data, tag: String) -> String? {
        guard let xml = String(data: data, encoding: .utf8) else { return nil }
        let pattern = "<\(tag)>(.+?)</\(tag)>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]) else { return nil }
        let range = NSRange(location: 0, length: (xml as NSString).length)
        guard let match = regex.firstMatch(in: xml, options: [], range: range), match.numberOfRanges >= 2 else { return nil }
        let valueRange = match.range(at: 1)
        return (xml as NSString).substring(with: valueRange).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - URLSessionDelegate (TLS handling + client cert)

extension NvHTTPClient: URLSessionDelegate {

    public func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let space = challenge.protectionSpace

        // Server certificate: Sunshine は自己署名 cert なので、 ペア済なら pinning で検証、
        // 未ペアならとりあえず信頼 (TOFU)。
        if space.authenticationMethod == NSURLAuthenticationMethodServerTrust {
            guard let trust = space.serverTrust else {
                completionHandler(.cancelAuthenticationChallenge, nil)
                return
            }
            // pinning ある時はそれと一致することを確認
            if let pinned = pinnedServerCertSHA256 {
                if let leaf = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
                   let first = leaf.first {
                    let fpt = sha256(SecCertificateCopyData(first) as Data)
                    if fpt == pinned {
                        // self-signed でも信頼するため exception を強制 set
                        if let ex = SecTrustCopyExceptions(trust) {
                            SecTrustSetExceptions(trust, ex)
                        }
                        completionHandler(.useCredential, URLCredential(trust: trust))
                        return
                    }
                }
                // pin 不一致 → 拒否
                completionHandler(.cancelAuthenticationChallenge, nil)
                return
            }
            // pinning 未設定 (初回 pairing 中、 もしくは TOFU) → 強制信頼。
            // SecTrustSetExceptions で self-signed cert の verification を bypass する。
            if let ex = SecTrustCopyExceptions(trust) {
                SecTrustSetExceptions(trust, ex)
            }
            completionHandler(.useCredential, URLCredential(trust: trust))
            return
        }

        // Client certificate: ペア済セッションでは Sunshine が要求してくる
        if space.authenticationMethod == NSURLAuthenticationMethodClientCertificate {
            guard let identity = clientIdentity else {
                // ペアリング前 (cert まだ無い) → そのまま続行 (Sunshine 側で適切に扱う)
                completionHandler(.performDefaultHandling, nil)
                return
            }
            let cred = URLCredential(identity: identity, certificates: nil, persistence: .forSession)
            completionHandler(.useCredential, cred)
            return
        }

        completionHandler(.performDefaultHandling, nil)
    }

    private func sha256(_ data: Data) -> Data {
        var hash = [UInt8](repeating: 0, count: 32) // SHA256_DIGEST_LENGTH
        data.withUnsafeBytes { (rawBuffer: UnsafeRawBufferPointer) -> Void in
            guard let ptr = rawBuffer.baseAddress else { return }
            // CommonCrypto 経由で SHA256 計算
            cc_sha256(ptr, data.count, &hash)
        }
        return Data(hash)
    }
}

// CommonCrypto を Swift から直接呼ぶための関数 (CommonCrypto は umbrella header
// として bridging で見えるので import なしで使える)
@_silgen_name("CC_SHA256")
private func cc_sha256(_ data: UnsafeRawPointer, _ len: Int, _ md: UnsafeMutablePointer<UInt8>) -> UnsafeMutablePointer<UInt8>?
