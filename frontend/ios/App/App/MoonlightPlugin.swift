// MoonlightPlugin.swift
//
// build 26 で「web 主導アーキテクチャ」 化。 plugin は generic な native 機能だけ expose する。
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

    private let moonlight = MoonlightBridge()
    private var streamView: UIView?
    private var closeButton: UIButton?

    public override func load() {
        NSLog("[MoonlightPlugin] loaded (build 26: web-driven architecture, generic plugin)")
        moonlight.setStatusCallback { [weak self] event, payload in
            var data: [String: Any] = ["event": event]
            for (k, v) in payload { data[k] = v }
            self?.notifyListeners("statusChange", data: data)
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

            // streamView を chat の上半分に重ねる layout (build 21 から維持)
            guard let parent = self.bridge?.viewController?.view else {
                call.reject("viewController.view が取得できません")
                return
            }
            if self.streamView == nil {
                let v = UIView()
                v.backgroundColor = .black
                v.translatesAutoresizingMaskIntoConstraints = false
                parent.addSubview(v)
                // build 30: 16:9 アスペクト比で高さ自動調整 (= Mac の 1920x1080 とフィット、 黒余白なし)。
                // 上端固定 + 横幅いっぱい + height = width * 9 / 16。
                let aspect = v.heightAnchor.constraint(equalTo: v.widthAnchor, multiplier: 9.0 / 16.0)
                aspect.priority = .defaultHigh  // = setVideoFrame で上書きできるよう priority 下げる
                NSLayoutConstraint.activate([
                    v.topAnchor.constraint(equalTo: parent.topAnchor),
                    v.leadingAnchor.constraint(equalTo: parent.leadingAnchor),
                    v.trailingAnchor.constraint(equalTo: parent.trailingAnchor),
                    aspect,
                ])
                self.streamView = v
            }
            if self.closeButton == nil, let view = self.streamView {
                let btn = UIButton(type: .system)
                btn.setTitle("✕", for: .normal)
                btn.setTitleColor(.white, for: .normal)
                btn.titleLabel?.font = .systemFont(ofSize: 22, weight: .bold)
                btn.backgroundColor = UIColor(white: 0, alpha: 0.5)
                btn.layer.cornerRadius = 18
                btn.translatesAutoresizingMaskIntoConstraints = false
                btn.addTarget(self, action: #selector(self.closeStreamTapped), for: .touchUpInside)
                parent.addSubview(btn)
                NSLayoutConstraint.activate([
                    btn.topAnchor.constraint(equalTo: view.topAnchor, constant: 12),
                    btn.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
                    btn.widthAnchor.constraint(equalToConstant: 36),
                    btn.heightAnchor.constraint(equalToConstant: 36),
                ])
                self.closeButton = btn
            }
            self.streamView?.isHidden = false
            self.closeButton?.isHidden = false
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
            self?.closeButton?.isHidden = true
        }
        call.resolve()
    }

    @objc private func closeStreamTapped() {
        moonlight.disconnect()
        DispatchQueue.main.async { [weak self] in
            self?.streamView?.isHidden = true
            self?.closeButton?.isHidden = true
        }
        notifyListeners("statusChange", data: ["event": "userClosed"])
    }

    /// stream view の frame を web から動的に指示。 build 30 で実装、 build 32 で WebView origin 補正追加。
    /// web から渡される (x, y) は WebView 内座標 (= getBoundingClientRect)、
    /// streamView は viewController.view の subview なので画面絶対座標が必要。
    /// WebView がキーボード退避で上シフトしても streamView を画面上端に固定するため、
    /// WebView の frame.origin を加算する。
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
            // 閉じる ✕ ボタンも追従
            self.closeButton?.frame.origin = CGPoint(x: absX + w - 48, y: absY + 12)
            call.resolve()
        }
    }

    @objc func getStatus(_ call: CAPPluginCall) {
        call.resolve(["state": "idle"])  // build 26 では JS 側で状態管理するので native は最小情報のみ
    }

    @objc func togglePiP(_ call: CAPPluginCall) {
        // 旧 API、 enablePiP / disablePiP に置換
        call.resolve(["active": false])
    }

    // MARK: - build 27 で追加: Phase 5 PiP

    @objc func enablePiP(_ call: CAPPluginCall) {
        let ok = moonlight.enablePiP()
        call.resolve(["started": ok])
    }

    @objc func disablePiP(_ call: CAPPluginCall) {
        moonlight.disablePiP()
        call.resolve()
    }

    // MARK: - build 27 で追加: Phase 5.5 全パソコン操作

    @objc func sendMouseMove(_ call: CAPPluginCall) {
        let dx = Int16(call.getInt("dx") ?? 0)
        let dy = Int16(call.getInt("dy") ?? 0)
        moonlight.sendMouseMove(dx: dx, dy: dy)
        call.resolve()
    }

    @objc func sendMousePosition(_ call: CAPPluginCall) {
        let x = Int16(call.getInt("x") ?? 0)
        let y = Int16(call.getInt("y") ?? 0)
        let refW = Int16(call.getInt("refW") ?? 1920)
        let refH = Int16(call.getInt("refH") ?? 1080)
        moonlight.sendMousePosition(x: x, y: y, refW: refW, refH: refH)
        call.resolve()
    }

    @objc func sendMouseButton(_ call: CAPPluginCall) {
        // button: "left"=1, "middle"=2, "right"=3, "x1"=4, "x2"=5
        let buttonStr = call.getString("button") ?? "left"
        let buttonMap: [String: UInt8] = ["left": 1, "middle": 2, "right": 3, "x1": 4, "x2": 5]
        let button = buttonMap[buttonStr] ?? 1
        // action: "press" / "release"
        let actionStr = call.getString("action") ?? "press"
        let action: UInt8 = (actionStr == "press") ? 0x07 : 0x08
        moonlight.sendMouseButton(button: button, action: action)
        call.resolve()
    }

    @objc func sendScroll(_ call: CAPPluginCall) {
        // -127..127 の範囲 (Int8)、 1 click = 1 notch
        let raw = call.getInt("delta") ?? 0
        let clamped = max(-127, min(127, raw))
        let delta = Int8(clamped)
        let horizontal = call.getBool("horizontal") ?? false
        if horizontal {
            moonlight.sendHScroll(delta: delta)
        } else {
            moonlight.sendScroll(delta: delta)
        }
        call.resolve()
    }

    @objc func sendKeyEvent(_ call: CAPPluginCall) {
        let keyCode = Int16(call.getInt("keyCode") ?? 0)
        let modifiers = UInt8(call.getInt("modifiers") ?? 0)
        // action: "down"=0x03 / "up"=0x04
        let actionStr = call.getString("action") ?? "down"
        let action: UInt8 = (actionStr == "down") ? 0x03 : 0x04
        moonlight.sendKey(keyCode: keyCode, modifiers: modifiers, action: action)
        call.resolve()
    }

    @objc func sendTouch(_ call: CAPPluginCall) {
        // eventType: "down"=0, "up"=1, "move"=2, "cancel"=3
        let typeMap: [String: UInt8] = ["down": 0, "up": 1, "move": 2, "cancel": 3]
        let eventType = typeMap[call.getString("eventType") ?? "move"] ?? 2
        let pointerId = UInt32(call.getInt("pointerId") ?? 0)
        let x = Float(call.getDouble("x") ?? 0)
        let y = Float(call.getDouble("y") ?? 0)
        let pressure = Float(call.getDouble("pressure") ?? 0)
        moonlight.sendTouch(eventType: eventType, pointerId: pointerId, x: x, y: y, pressure: pressure)
        call.resolve()
    }

    // MARK: - build 27 で追加: Phase 6 一部 (Haptic + Face ID)

    @objc func haptic(_ call: CAPPluginCall) {
        let pattern = call.getString("pattern") ?? "light"
        moonlight.haptic(pattern: pattern)
        call.resolve()
    }

    // authenticate は削除: iOS 16+ の標準「アプリを Face ID でロック」 で代替

    // MARK: - getStats (= 遅延 overlay 用、 リアルタイム RTT/fps/kbps/codec)
    @objc func getStats(_ call: CAPPluginCall) {
        let stats = moonlight.getStats()
        call.resolve(stats)
    }

    // MARK: - 追加入力 (build 27 完成版)

    @objc func sendUtf8Text(_ call: CAPPluginCall) {
        let text = call.getString("text") ?? ""
        moonlight.sendUtf8Text(text)
        call.resolve()
    }

    @objc func sendHighResScroll(_ call: CAPPluginCall) {
        let raw = call.getInt("delta") ?? 0
        let clamped = max(-32768, min(32767, raw))
        let delta = Int16(clamped)
        let horizontal = call.getBool("horizontal") ?? false
        if horizontal { moonlight.sendHighResHScroll(delta: delta) }
        else { moonlight.sendHighResScroll(delta: delta) }
        call.resolve()
    }

    @objc func requestIdrFrame(_ call: CAPPluginCall) {
        moonlight.requestIdrFrame()
        call.resolve()
    }

    @objc func interrupt(_ call: CAPPluginCall) {
        moonlight.interrupt()
        call.resolve()
    }

    // MARK: - 画面回転 lock / unlock (= Phase 7 用)
    @objc func setOrientationLock(_ call: CAPPluginCall) {
        let orient = call.getString("orientation") ?? "auto"
        HavenOrientation.locked = orient
        DispatchQueue.main.async {
            // iOS 16+ では setNeedsUpdateOfSupportedInterfaceOrientations、 古い iOS では attemptRotation
            if #available(iOS 16.0, *) {
                self.bridge?.viewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
            } else {
                UIViewController.attemptRotationToDeviceOrientation()
            }
        }
        call.resolve()
    }
}
