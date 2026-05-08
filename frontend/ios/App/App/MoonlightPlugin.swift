// MoonlightPlugin.swift
//
// 「web 主導アーキテクチャ」: plugin は generic な native 機能だけ expose する。
// 接続フローと値計算は frontend/src/native/moonlight-flow.js に移行。
//
// 公開メソッド:
//   - pair({host, pin}) → SecIdentity 操作が要るので native のまま
//   - request({path, params, useTLS}) → 生 HTTP に client cert 載せて XML 文字列返す
//   - startStream({host, appVersion, gfeVersion, ...}) → LiStartConnection を JS の指示で起動
//   - disconnect() → LiStopConnection
//   - getStatus() → 現状の state
//   - togglePiP() → Phase 5 で実装、 当面 stub
//
// 状態通知 (statusChange イベント):
//   - {event: "stageStarting"|"stageComplete"|"stageFailed", name: string, code?: number, ...}
//   - {event: "connectionStarted"|"connectionTerminated"|"videoContentShown"|...}
//   JS 側の addListener('statusChange', cb) で受ける。

import Capacitor
import UIKit

@objc(MoonlightPlugin)
public class MoonlightPlugin: CAPPlugin {

    // MoonlightInputBridge.swift extension からも触る必要があるので internal 可視性 (= デフォルト)。
    let moonlight = MoonlightBridge()
    private var streamView: UIView?
    // ✕ 閉じるボタンは無し (= web 側 🖥 トグルで disconnect、 UI 邪魔回避)。
    // 代わりに statusLabel を streamView に被せて native 側で stream 進捗を表示。
    private var statusLabel: UILabel?
    private var statusHideTimer: Timer?

    public override func load() {
        NSLog("[MoonlightPlugin] loaded")
        moonlight.setStatusCallback { [weak self] event, payload in
            var data: [String: Any] = ["event": event]
            for (k, v) in payload { data[k] = v }
            self?.notifyListeners("statusChange", data: data)
            // native 側でも streamView 上の overlay label を更新 (= web 側 stream-status-line の代替)
            self?.updateStatusOverlay(event: event, payload: payload)
        }
    }

    /// streamView の上端中央に半透明の status pill を被せる。 stream の stage 進行 / 接続表示 /
    /// 切断・失敗を user にフィードバック。 「videoContentShown」 で 1.5 秒後に自動非表示。
    private func updateStatusOverlay(event: String, payload: [String: Any]) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, let view = self.streamView else { return }
            if self.statusLabel == nil {
                let label = UILabel()
                label.translatesAutoresizingMaskIntoConstraints = false
                label.font = .systemFont(ofSize: 13, weight: .medium)
                label.textColor = .white
                label.backgroundColor = UIColor(white: 0, alpha: 0.55)
                label.textAlignment = .center
                label.layer.cornerRadius = 12
                label.layer.masksToBounds = true
                label.numberOfLines = 1
                label.isUserInteractionEnabled = false
                view.addSubview(label)
                NSLayoutConstraint.activate([
                    label.topAnchor.constraint(equalTo: view.topAnchor, constant: 8),
                    label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
                    label.heightAnchor.constraint(equalToConstant: 24),
                ])
                // 横幅は内容に応じて (= padding 込み)、 ただし streamView の 80% を上限
                label.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, multiplier: 0.8).isActive = true
                self.statusLabel = label
            }

            // event → 表示文字
            var text: String? = nil
            var hideAfter: TimeInterval = 0  // 0 = 自動非表示しない
            switch event {
            case "stageStarting":
                if let name = payload["name"] as? String { text = "⏳ \(name) …" }
            case "stageComplete":
                if let name = payload["name"] as? String { text = "✓ \(name)" }
            case "stageFailed":
                let name = (payload["name"] as? String) ?? "stage"
                let code = (payload["code"] as? Int) ?? 0
                text = "⚠ \(name) 失敗 (code=\(code))"
                hideAfter = 4.0
            case "connectionStarted":
                text = "接続確立、 frame 待機…"
            case "videoContentShown":
                text = "✓ 表示中"
                hideAfter = 1.5
            case "connectionTerminated":
                let code = (payload["code"] as? Int) ?? 0
                text = "切断 (code=\(code))"
                hideAfter = 3.0
            case "userClosed":
                self.statusLabel?.isHidden = true
                self.statusHideTimer?.invalidate()
                return
            default:
                return  // 他 event は overlay 更新しない (= getStats / pip 等)
            }

            if let t = text {
                self.statusLabel?.text = "  \(t)  "
                self.statusLabel?.isHidden = false
                self.statusHideTimer?.invalidate()
                if hideAfter > 0 {
                    self.statusHideTimer = Timer.scheduledTimer(withTimeInterval: hideAfter, repeats: false) { [weak self] _ in
                        self?.statusLabel?.isHidden = true
                    }
                }
            }
        }
    }

    @objc func pair(_ call: CAPPluginCall) {
        guard let host = call.getString("host"), !host.isEmpty,
              let pin = call.getString("pin"), !pin.isEmpty else {
            call.reject("host と pin は必須です")
            return
        }
        moonlight.pair(host: host, pin: pin) { result in
            switch result {
            case .success: call.resolve(["paired": true])
            case .failure(let err): call.reject("pair failed: \(err)")
            }
        }
    }

    /// 生 HTTP を JS から実行 (client cert 付き)。 XML 文字列 (= body) を返す。
    @objc func request(_ call: CAPPluginCall) {
        let host = call.getString("host")
        guard let path = call.getString("path"), !path.isEmpty else {
            call.reject("path は必須です")
            return
        }
        let params = call.getObject("params") as? [String: String] ?? [:]
        let useTLS = call.getBool("useTLS") ?? true

        moonlight.request(host: host, path: path, params: params, useTLS: useTLS) { result in
            switch result {
            case .success(let body):
                call.resolve(["body": body])
            case .failure(let err):
                call.reject("request failed: \(err)")
            }
        }
    }

    /// LiStartConnection を起動。 streamView の貼り付けもここで行う。
    @objc func startStream(_ call: CAPPluginCall) {
        guard let host = call.getString("host"), !host.isEmpty else {
            call.reject("host は必須です")
            return
        }

        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }

            // streamView を chat の上半分に重ねる layout
            guard let parent = self.bridge?.viewController?.view else {
                call.reject("viewController.view が取得できません")
                return
            }
            if self.streamView == nil {
                let v = StreamHostView()
                v.backgroundColor = .black
                v.translatesAutoresizingMaskIntoConstraints = false
                // streamView の touch event を WebView に pass-through (= web 側 overlay
                // div で touch 受けて plugin の sendMouseMove 等を呼ぶ設計)。
                // streamView 自体は描画だけ、 入力は web 側ジェスチャ層が処理する。
                v.isUserInteractionEnabled = false
                parent.addSubview(v)
                // 初期 layout: safeAreaLayoutGuide.topAnchor 起点 + width/9*16 の 16:9 矩形。
                // 接続瞬間の setVideoFrame 反映前でも status bar を侵食しない位置に置く。
                // web 側 stream-overlay div の位置に setVideoFrame で追従更新される
                // (= キーボード on の間は引数を画面上端固定に切替、 入力欄が隠れない)。
                let aspect = v.heightAnchor.constraint(equalTo: v.widthAnchor, multiplier: 9.0 / 16.0)
                aspect.priority = .defaultHigh  // setVideoFrame で上書き可能
                NSLayoutConstraint.activate([
                    v.topAnchor.constraint(equalTo: parent.safeAreaLayoutGuide.topAnchor),
                    v.leadingAnchor.constraint(equalTo: parent.leadingAnchor),
                    v.trailingAnchor.constraint(equalTo: parent.trailingAnchor),
                    aspect,
                ])
                self.streamView = v
            }
            self.streamView?.isHidden = false
            self.moonlight.streamView = self.streamView

            // JS から渡された全パラメータを config dict としてそのまま渡す
            var cfg: [String: Any] = [
                "host": host,
                "appVersion": call.getString("appVersion") ?? "",
                "gfeVersion": call.getString("gfeVersion") ?? "",
                "rtspSessionUrl": call.getString("rtspSessionUrl") ?? "",
                "riKeyHex": call.getString("riKeyHex") ?? "",
                "serverCodecModeSupport": NSNumber(value: call.getInt("serverCodecModeSupport") ?? 0),
            ]
            if let n = call.getInt("riKeyId") { cfg["riKeyId"] = NSNumber(value: n) }
            if let n = call.getInt("width") { cfg["width"] = NSNumber(value: n) }
            if let n = call.getInt("height") { cfg["height"] = NSNumber(value: n) }
            if let n = call.getInt("fps") { cfg["fps"] = NSNumber(value: n) }
            if let n = call.getInt("bitrate") { cfg["bitrate"] = NSNumber(value: n) }
            if let n = call.getInt("audioConfig") { cfg["audioConfig"] = NSNumber(value: n) }
            if let n = call.getInt("supportedVideoFormats") { cfg["supportedVideoFormats"] = NSNumber(value: n) }
            if let b = call.getBool("useFramePacing") { cfg["useFramePacing"] = NSNumber(value: b) }

            self.moonlight.startStream(config: cfg) { result in
                switch result {
                case .success: call.resolve(["started": true])
                case .failure(let err): call.reject("startStream failed: \(err)")
                }
            }
        }
    }

    @objc func disconnect(_ call: CAPPluginCall) {
        moonlight.disconnect()
        DispatchQueue.main.async { [weak self] in
            self?.streamView?.isHidden = true
            self?.statusLabel?.isHidden = true
            self?.statusHideTimer?.invalidate()
        }
        call.resolve()
    }

    /// stream view の frame を web から動的に指示。 web から渡される (x, y) は WebView 内
    /// 座標 (= getBoundingClientRect)、 streamView は viewController.view の subview なので
    /// 画面絶対座標が必要。 WebView の frame.origin を加算して座標変換する。
    @objc func setVideoFrame(_ call: CAPPluginCall) {
        let x = CGFloat(call.getDouble("x") ?? 0)
        let y = CGFloat(call.getDouble("y") ?? 0)
        let w = CGFloat(call.getDouble("width") ?? 0)
        let h = CGFloat(call.getDouble("height") ?? 0)
        DispatchQueue.main.async { [weak self] in
            guard let self = self, let v = self.streamView else { call.resolve(); return }
            // WebView の origin を取得 (= キーボード退避で上シフトしてる量)。
            // 渡された WebView 内座標を画面絶対座標に変換する。
            let webOriginX: CGFloat = self.bridge?.webView?.frame.origin.x ?? 0
            let webOriginY: CGFloat = self.bridge?.webView?.frame.origin.y ?? 0
            let absX = x + webOriginX
            let absY = y + webOriginY

            v.translatesAutoresizingMaskIntoConstraints = true
            if let parent = v.superview {
                parent.constraints.filter { $0.firstItem === v || $0.secondItem === v }
                    .forEach { parent.removeConstraint($0) }
            }
            v.constraints.forEach { v.removeConstraint($0) }
            v.frame = CGRect(x: absX, y: absY, width: w, height: h)
            HavenDebugLog("MoonlightPlugin::setVideoFrame" as NSString,
                          "in=(\(x),\(y),\(w),\(h)) webOrigin=(\(webOriginX),\(webOriginY)) abs=(\(absX),\(absY)) v.frame=\(v.frame) v.bounds=\(v.bounds)" as NSString)
            call.resolve()
        }
    }

    @objc func getStatus(_ call: CAPPluginCall) {
        call.resolve(["state": "idle"])  // 状態は JS 側で管理 (= web 主導)、 native は最小情報のみ
    }

    // MARK: - Phase 5 PiP

    @objc func enablePiP(_ call: CAPPluginCall) {
        let ok = moonlight.enablePiP()
        call.resolve(["started": ok])
    }

    @objc func disablePiP(_ call: CAPPluginCall) {
        moonlight.disablePiP()
        call.resolve()
    }

    // 入力 / 観測系メソッド (= Phase 5.5 全パソコン操作 / Haptic / getStats /
    // 追加入力 / 画面回転 lock) は MoonlightInputBridge.swift に extension で分離。
}
