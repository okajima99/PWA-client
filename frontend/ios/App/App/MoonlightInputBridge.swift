// MoonlightPlugin の入力 / 観測系メソッド (= mouse / keyboard / touch / scroll /
// haptic / getStats / IDR 再要求 / interrupt / orientation lock) の extension。
// MoonlightPlugin.swift から責務分離 (= core proxy + UI overlay と入力 plumbing を切り離す)。
//
// MoonlightPlugin.m の CAP_PLUGIN_METHOD で全 method を symbol 登録済、 Capacitor は
// 実装ファイルの位置に依存せず @objc method を解決する。

import Capacitor
import UIKit

extension MoonlightPlugin {
    // MARK: - Phase 5.5 全パソコン操作

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

    // MARK: - Phase 6 一部 (Haptic)

    @objc func haptic(_ call: CAPPluginCall) {
        let pattern = call.getString("pattern") ?? "light"
        moonlight.haptic(pattern: pattern)
        call.resolve()
    }

    // MARK: - getStats (= 遅延 overlay 用、 リアルタイム RTT/fps/kbps/codec)
    @objc func getStats(_ call: CAPPluginCall) {
        let stats = moonlight.getStats()
        call.resolve(stats)
    }

    // MARK: - 追加入力

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
