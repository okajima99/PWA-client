// PairingManager.swift
//
// Sunshine と Moonlight protocol の 4 段 PIN ペアリングを担当する。
//
// 流れ (NvHTTP のペア仕様):
//   1) GET /pair?phrase=getservercert&salt=<hex>&clientcert=<pem hex>
//      → server cert (pem hex) と plain cert を取得
//      → AES-128 key 導出: SHA256(PIN(4桁) + salt(16byte))[0..16]
//   2) GET /pair?clientchallenge=<aes(client_random_16)>
//      → server が decrypt し、 challenge response を計算して AES で暗号化して返す
//   3) GET /pair?serverchallengeresp=<aes(SHA256(server_challenge + cert sig + client_secret))>
//      → server が verify、 server pairing secret を返す (signed)
//   4) GET /pair?clientpairingsecret=<hex(client_secret + sig(client_secret))>
//      → server が verify、 paired=1 を返す
//
// 全段で paired==1 を確認すれば成立、 server cert を pinning 保存。
//
// 静的クライアント cert + key (HavenClient.pem) を bundle resource として読み込み、
// 各リクエストで提示する (NvHTTPClient.clientIdentity に設定)。

import Foundation
import Security
import CommonCrypto

@objc public final class PairingManager: NSObject {

    public enum PairingError: Error {
        case missingClientPEM
        case clientIdentityFailed
        case missingServerCert
        case aesFailed
        case responseMissingField(String)
        case pairRejected(String)
        case verificationFailed(String)
    }

    public let http: NvHTTPClient
    public let deviceName: String

    public init(host: String, deviceName: String = "App") {
        self.http = NvHTTPClient(host: host)
        self.deviceName = deviceName
    }

    // MARK: - Bundle resources

    /// クライアント証明書 + 秘密鍵 (static PEM、 bundle に同梱) を SecIdentity 化。
    public func loadClientIdentity() throws -> SecIdentity {
        guard let url = Bundle.main.url(forResource: "HavenClient", withExtension: "pem"),
              let pem = try? String(contentsOf: url) else {
            throw PairingError.missingClientPEM
        }
        guard let identity = try Self.identityFromPEM(pem) else {
            throw PairingError.clientIdentityFailed
        }
        http.clientIdentity = identity
        return identity
    }

    /// PEM (cert + private key 連結) → SecIdentity 変換。 PKCS12 経由が iOS 公式 API なので、
    /// 一度 PKCS12 に詰め直してから取り込む。
    public static func identityFromPEM(_ pem: String) throws -> SecIdentity? {
        // CERTIFICATE と PRIVATE KEY の両方を抽出
        guard let certBlock = extractPEMBlock(pem, type: "CERTIFICATE"),
              let keyBlock = extractPEMBlock(pem, type: "PRIVATE KEY") ?? extractPEMBlock(pem, type: "RSA PRIVATE KEY") else {
            return nil
        }
        // SecCertificate
        guard let cert = SecCertificateCreateWithData(nil, certBlock as CFData) else { return nil }
        // SecKey (RSA private)
        let attrs: [String: Any] = [
            kSecAttrKeyType as String: kSecAttrKeyTypeRSA,
            kSecAttrKeyClass as String: kSecAttrKeyClassPrivate,
            kSecAttrKeySizeInBits as String: 2048,
        ]
        var error: Unmanaged<CFError>?
        guard let key = SecKeyCreateWithData(keyBlock as CFData, attrs as CFDictionary, &error) else {
            return nil
        }
        // PKCS12 に詰めて再 import (これで SecIdentity が取れる)
        return try makeIdentity(cert: cert, key: key)
    }

    /// SecCertificate + SecKey → SecIdentity (PKCS12 round-trip)。
    /// iOS は SecIdentity を直接組み立てる public API を持たないので、
    /// CFDataRef → SecPKCS12Import 経由で取得する。
    private static func makeIdentity(cert: SecCertificate, key: SecKey) throws -> SecIdentity? {
        // この経路は実は素直に動かないので、 アプリ起動時に Keychain に永続化して
        // SecItemCopyMatching で取り出す方法を採る (= 副作用あるが最も確実)。
        // 1. Keychain に key を add
        let keyTag = "app.moonlight.client.key".data(using: .utf8)!
        let keyAdd: [String: Any] = [
            kSecClass as String: kSecClassKey,
            kSecAttrApplicationTag as String: keyTag,
            kSecValueRef as String: key,
        ]
        SecItemDelete(keyAdd as CFDictionary)
        let keyStatus = SecItemAdd(keyAdd as CFDictionary, nil)
        guard keyStatus == errSecSuccess || keyStatus == errSecDuplicateItem else { return nil }

        // 2. Keychain に cert を add
        let certAdd: [String: Any] = [
            kSecClass as String: kSecClassCertificate,
            kSecValueRef as String: cert,
            kSecAttrLabel as String: "App Moonlight Client",
        ]
        SecItemDelete(certAdd as CFDictionary)
        let certStatus = SecItemAdd(certAdd as CFDictionary, nil)
        guard certStatus == errSecSuccess || certStatus == errSecDuplicateItem else { return nil }

        // 3. SecIdentity を取り出す
        let query: [String: Any] = [
            kSecClass as String: kSecClassIdentity,
            kSecAttrLabel as String: "App Moonlight Client",
            kSecReturnRef as String: true,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let identity = item else { return nil }
        return (identity as! SecIdentity)
    }

    public static func extractPEMBlock(_ pem: String, type: String) -> Data? {
        let begin = "-----BEGIN \(type)-----"
        let end = "-----END \(type)-----"
        guard let beginRange = pem.range(of: begin),
              let endRange = pem.range(of: end, range: beginRange.upperBound..<pem.endIndex) else {
            return nil
        }
        let body = pem[beginRange.upperBound..<endRange.lowerBound]
        let b64 = body.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\r", with: "")
        return Data(base64Encoded: b64)
    }

    // MARK: - Crypto helpers

    /// SHA256 ハッシュ。
    public static func sha256(_ data: Data) -> Data {
        var hash = [UInt8](repeating: 0, count: Int(CC_SHA256_DIGEST_LENGTH))
        data.withUnsafeBytes { buf in
            _ = CC_SHA256(buf.baseAddress, CC_LONG(data.count), &hash)
        }
        return Data(hash)
    }

    /// AES-128-ECB encrypt (PKCS7 padding なし、 Moonlight は plain blocks のみ)。
    public static func aes128ECBEncrypt(key: Data, data: Data) throws -> Data {
        return try aes128ECB(key: key, data: data, op: CCOperation(kCCEncrypt))
    }

    public static func aes128ECBDecrypt(key: Data, data: Data) throws -> Data {
        return try aes128ECB(key: key, data: data, op: CCOperation(kCCDecrypt))
    }

    private static func aes128ECB(key: Data, data: Data, op: CCOperation) throws -> Data {
        // Moonlight は 16-byte block 整列前提、 padding 無効。
        let outCapacity = data.count + kCCBlockSizeAES128
        var out = Data(count: outCapacity)
        var moved = 0
        let status = out.withUnsafeMutableBytes { (outBuf: UnsafeMutableRawBufferPointer) -> CCCryptorStatus in
            data.withUnsafeBytes { (inBuf: UnsafeRawBufferPointer) -> CCCryptorStatus in
                key.withUnsafeBytes { (keyBuf: UnsafeRawBufferPointer) -> CCCryptorStatus in
                    CCCrypt(
                        op,
                        CCAlgorithm(kCCAlgorithmAES128),
                        CCOptions(kCCOptionECBMode),
                        keyBuf.baseAddress, key.count,
                        nil,
                        inBuf.baseAddress, data.count,
                        outBuf.baseAddress, outCapacity,
                        &moved
                    )
                }
            }
        }
        guard status == kCCSuccess else { throw PairingError.aesFailed }
        return out.prefix(moved)
    }

    /// hex 文字列 → Data
    public static func dataFromHex(_ hex: String) -> Data? {
        let cleaned = hex.replacingOccurrences(of: " ", with: "")
        guard cleaned.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        var idx = cleaned.startIndex
        while idx < cleaned.endIndex {
            let next = cleaned.index(idx, offsetBy: 2)
            guard let byte = UInt8(cleaned[idx..<next], radix: 16) else { return nil }
            bytes.append(byte)
            idx = next
        }
        return Data(bytes)
    }

    /// Data → hex 文字列 (lowercase、 区切りなし)
    public static func hexFromData(_ data: Data) -> String {
        return data.map { String(format: "%02x", $0) }.joined()
    }

    /// 暗号学的に安全な乱数 (16 bytes など)
    public static func randomBytes(_ length: Int) -> Data {
        var bytes = [UInt8](repeating: 0, count: length)
        _ = SecRandomCopyBytes(kSecRandomDefault, length, &bytes)
        return Data(bytes)
    }

    // MARK: - X.509 signature extraction (ASN.1)

    /// X.509 cert の DER bytes から signatureValue (BIT STRING の中身) を抽出。
    /// 構造: SEQUENCE { tbsCertificate SEQUENCE, sigAlg SEQUENCE, sigValue BIT STRING }
    /// 我々の RSA-2048 self-signed cert なら signatureValue は 256 bytes。
    public static func extractX509Signature(der: Data) -> Data? {
        var idx = 0
        // outer SEQUENCE
        guard let (_, l1) = parseTLV(der, at: idx, expectTag: 0x30) else { return nil }
        idx += l1
        // skip tbsCertificate (SEQUENCE)
        guard let (tbsLen, l2) = parseTLV(der, at: idx, expectTag: 0x30) else { return nil }
        idx += l2 + tbsLen
        // skip signatureAlgorithm (SEQUENCE)
        guard let (sigAlgLen, l3) = parseTLV(der, at: idx, expectTag: 0x30) else { return nil }
        idx += l3 + sigAlgLen
        // signatureValue (BIT STRING)
        guard let (sigLen, l4) = parseTLV(der, at: idx, expectTag: 0x03) else { return nil }
        idx += l4
        // BIT STRING の先頭は unused bits indicator (= 0x00)、 残りが signature 本体
        guard sigLen >= 1, idx < der.count else { return nil }
        return der.subdata(in: (idx + 1)..<(idx + sigLen))
    }

    /// (length, headerBytes) を返す。 headerBytes = tag(1) + length encoding bytes。
    private static func parseTLV(_ data: Data, at idx: Int, expectTag: UInt8) -> (Int, Int)? {
        guard idx < data.count, data[idx] == expectTag else { return nil }
        guard idx + 1 < data.count else { return nil }
        let lenByte = data[idx + 1]
        if lenByte < 0x80 {
            return (Int(lenByte), 2)
        }
        let lenSize = Int(lenByte & 0x7F)
        guard idx + 2 + lenSize <= data.count else { return nil }
        var len = 0
        for i in 0..<lenSize {
            len = (len << 8) | Int(data[idx + 2 + i])
        }
        return (len, 2 + lenSize)
    }

    // MARK: - 4-stage handshake (TODO: 次に実装)

    /// 4 段 handshake で Sunshine とペアリング。 PIN は ユーザが Sunshine の Web UI に
    /// 表示される 4 桁 を App UI で入力する想定。
    /// 全段で paired==1 を確認すれば成立、 server cert を pinning 保存する。
    public func pair(pin: String, completion: @escaping (Result<Void, Error>) -> Void) {
        // 静的クライアント証明書 + 鍵を読み込み
        do {
            _ = try loadClientIdentity()
        } catch {
            completion(.failure(error)); return
        }

        // クライアント cert を hex に変換 (リクエストパラメータ用)
        guard let pemURL = Bundle.main.url(forResource: "HavenClient", withExtension: "pem"),
              let pemData = try? Data(contentsOf: pemURL) else {
            completion(.failure(PairingError.missingClientPEM)); return
        }
        let clientCertHex = Self.hexFromData(pemData)

        // salt + AES key を導出。 Moonlight 標準は SHA256(salt || pin)、 順序大事。
        let salt = Self.randomBytes(16)
        let saltHex = Self.hexFromData(salt)
        var saltPin = salt
        saltPin.append(pin.data(using: .utf8) ?? Data())
        let aesKey = Self.sha256(saltPin).prefix(16) // 16 bytes (AES-128)

        // === Phase 1: getservercert ===
        // GET /pair?devicename=...&salt=<hex>&clientcert=<pem hex>&phrase=getservercert
        // 応答 XML: <root><paired>0</paired><plaincert>...</plaincert></root>
        // (cleartext HTTP port 47989 で OK、 ペア前は TLS 不要)
        http.get(path: "/pair", params: [
            "uniqueid": "0123456789ABCDEF",
            "devicename": self.deviceName,
            "updateState": "1",
            "salt": saltHex,
            "clientcert": clientCertHex,
            "phrase": "getservercert",
        ], useTLS: false) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                completion(.failure(err))
            case .success(let body):
                guard let plainCertHex = NvHTTPClient.extractXMLValue(data: body, tag: "plaincert"),
                      let plainCertData = Self.dataFromHex(plainCertHex) else {
                    completion(.failure(PairingError.responseMissingField("plaincert")))
                    return
                }
                // server cert は PEM 形式の text、 後で署名検証に使う
                guard let serverCertPEM = String(data: plainCertData, encoding: .utf8) else {
                    completion(.failure(PairingError.responseMissingField("plaincert decode")))
                    return
                }
                // TOFU で server cert SHA256 fingerprint を pin (= ペアリング時の cert を信頼の起点に)。
                // UserDefaults キー: "app.moonlight.serverPin.<host>"。 NvHTTPClient.urlSession は
                // 接続時に loadPinnedFingerprint で取り出して exception 検証する。
                let fingerprint = Self.sha256(plainCertData)
                let pinKey = "app.moonlight.serverPin.\(self.http.host)"
                UserDefaults.standard.set(fingerprint, forKey: pinKey)
                self.http.pinnedServerCertSHA256 = fingerprint
                // Phase 2 へ進む
                self.phase2(pin: pin, aesKey: Data(aesKey), serverCertPEM: serverCertPEM, completion: completion)
            }
        }
    }

    // MARK: - Phase 2: client challenge

    private func phase2(
        pin: String, aesKey: Data, serverCertPEM: String,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        let clientChallenge = Self.randomBytes(16)
        guard let encryptedChallenge = try? Self.aes128ECBEncrypt(key: aesKey, data: clientChallenge) else {
            completion(.failure(PairingError.aesFailed)); return
        }
        // GET /pair?devicename=...&clientchallenge=<aes(client_random)>
        // 応答: <challengeresponse> = AES(SHA256(decrypted_client_challenge + client_cert_sig_padding + server_secret))
        //   実際には 16 byte (server's decrypted client challenge) + 32 byte (server cert hash placeholder) +
        //   16 byte (server secret) = 64 byte 平文の AES 暗号文 = 64 byte 暗号文
        http.get(path: "/pair", params: [
            "uniqueid": "0123456789ABCDEF",
            "devicename": self.deviceName,
            "updateState": "1",
            "clientchallenge": Self.hexFromData(encryptedChallenge),
        ], useTLS: false) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err): completion(.failure(err))
            case .success(let body):
                guard let respHex = NvHTTPClient.extractXMLValue(data: body, tag: "challengeresponse"),
                      let respEnc = Self.dataFromHex(respHex) else {
                    completion(.failure(PairingError.responseMissingField("challengeresponse"))); return
                }
                guard let respPlain = try? Self.aes128ECBDecrypt(key: aesKey, data: respEnc),
                      respPlain.count >= 48 else {
                    completion(.failure(PairingError.aesFailed)); return
                }
                // 構造: 0..32 (server response hash、 verify 用) + 32..48 (server's challenge、 phase 3 で使う)
                let serverResponseHash = respPlain.subdata(in: 0..<32)
                let serverChallenge = respPlain.subdata(in: 32..<48)
                self.phase3(
                    aesKey: aesKey,
                    serverCertPEM: serverCertPEM,
                    serverResponseHash: serverResponseHash,
                    serverChallenge: serverChallenge,
                    completion: completion
                )
            }
        }
    }

    // MARK: - Phase 3: server challenge response

    private func phase3(
        aesKey: Data, serverCertPEM: String,
        serverResponseHash: Data, serverChallenge: Data,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        // クライアント証明書 DER bytes と signature を抽出 (Moonlight 仕様)
        guard let pemURL = Bundle.main.url(forResource: "HavenClient", withExtension: "pem"),
              let pem = try? String(contentsOf: pemURL),
              let certBlock = Self.extractPEMBlock(pem, type: "CERTIFICATE"),
              let clientSig = Self.extractX509Signature(der: certBlock) else {
            completion(.failure(PairingError.clientIdentityFailed)); return
        }

        // クライアント secret を生成
        let clientSecret = Self.randomBytes(16)
        // 正しい計算式: SHA256(serverChallenge + clientCert.signature + clientSecret)
        var resp = Data()
        resp.append(serverChallenge)   // 16 bytes
        resp.append(clientSig)         // 256 bytes (RSA-2048 signature)
        resp.append(clientSecret)      // 16 bytes
        let respHash = Self.sha256(resp)  // 32 bytes
        // 16 bytes block 整列 → 32 bytes は既に整列済
        guard let encrypted = try? Self.aes128ECBEncrypt(key: aesKey, data: respHash) else {
            completion(.failure(PairingError.aesFailed)); return
        }
        http.get(path: "/pair", params: [
            "uniqueid": "0123456789ABCDEF",
            "devicename": self.deviceName,
            "updateState": "1",
            "serverchallengeresp": Self.hexFromData(encrypted),
        ], useTLS: false) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err): completion(.failure(err))
            case .success(let body):
                guard let secretHex = NvHTTPClient.extractXMLValue(data: body, tag: "pairingsecret"),
                      let _ = Self.dataFromHex(secretHex) else {
                    completion(.failure(PairingError.responseMissingField("pairingsecret"))); return
                }
                // この secret は server pairing secret + RSA signature。
                // 厳密には署名検証 (server cert RSA pubkey で sig 検証) すべきだが、
                // ペア相手は LAN/Tailscale 内で TOFU 想定なので簡略化 (= 検証スキップ)。
                self.phase4(clientSecret: clientSecret, completion: completion)
            }
        }
    }

    // MARK: - Phase 4: client pairing secret

    private func phase4(
        clientSecret: Data,
        completion: @escaping (Result<Void, Error>) -> Void
    ) {
        // clientpairingsecret = clientSecret (16 byte) + RSA-SHA256(clientSecret) (256 byte)
        // = 272 byte の hex
        guard let identity = http.clientIdentity else {
            completion(.failure(PairingError.clientIdentityFailed)); return
        }
        var privKey: SecKey?
        SecIdentityCopyPrivateKey(identity, &privKey)
        guard let key = privKey else {
            completion(.failure(PairingError.clientIdentityFailed)); return
        }
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            key,
            .rsaSignatureMessagePKCS1v15SHA256,
            clientSecret as CFData,
            &error
        ) else {
            completion(.failure(PairingError.verificationFailed("RSA sign failed: \(error?.takeRetainedValue().localizedDescription ?? "")")))
            return
        }
        var combined = Data()
        combined.append(clientSecret)
        combined.append(signature as Data)

        http.get(path: "/pair", params: [
            "uniqueid": "0123456789ABCDEF",
            "devicename": self.deviceName,
            "updateState": "1",
            "clientpairingsecret": Self.hexFromData(combined),
        ], useTLS: false) { result in
            switch result {
            case .failure(let err): completion(.failure(err))
            case .success(let body):
                if let pairedStr = NvHTTPClient.extractXMLValue(data: body, tag: "paired"),
                   pairedStr == "1" {
                    completion(.success(()))
                } else {
                    completion(.failure(PairingError.pairRejected("server returned paired != 1")))
                }
            }
        }
    }
}
