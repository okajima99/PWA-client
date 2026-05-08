// DebugLog.swift
//
// iOS 18 + libimobiledevice の組み合わせで NSLog が idevicesyslog に流れない問題を
// 回避するための簡易 logging。 backend の `POST /debug/log` に投げて
// `/tmp/app-debug.log` に集約してもらい、 ARK が直接読む。
//
// 使い方:
//   DebugLog.send("NvHTTP", "GET https://...")
//   DebugLog.send("MoonlightBridge", "launching app id=...")
//
// fire-and-forget。 失敗しても本流に影響させない。

import Foundation

@objc public final class DebugLog: NSObject {

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        cfg.timeoutIntervalForResource = 10
        return URLSession(configuration: cfg)
    }()

    /// backend の HTTPS endpoint。 Tailscale Let's Encrypt cert なので self-signed exception 不要。
    /// VITE_NATIVE_API_BASE と同じ値 (= 現状ハードコード、 必要なら設定化)。
    private static let endpoint = URL(string: "https://user.tailnet.ts.net/debug/log")!

    @objc public static func send(_ tag: String, _ message: String) {
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = ["tag": tag, "message": message]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        session.dataTask(with: req) { _, _, _ in }.resume()
        // 同時に NSLog にも残す (Console.app から見たい時用、 副作用のみ)
        NSLog("[%@] %@", tag, message)
    }
}

// Obj-C / C から呼べる C 関数 wrapper (= 公式 Connection.m に observer 行を入れるため)。
// Obj-C / C から呼べる C 関数 wrapper: LiStartConnection の前後で観測 log を入れる用。
@_cdecl("HavenDebugLog")
public func HavenDebugLog(_ tag: NSString, _ message: NSString) {
    DebugLog.send(tag as String, message as String)
}
