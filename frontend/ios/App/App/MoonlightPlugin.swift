// MoonlightPlugin.swift
//
// Capacitor plugin。 Phase 3 で公式 moonlight-ios (GPL-3.0) の core を統合する
// 過渡期。 自前実装 (MoonlightBridge / NvHTTPClient / PairingManager) は
// _legacy_self_implementation/ に退避済、 公式統合完了まで Plugin は stub。
//
// JS 側 API は維持 (registerPlugin('Moonlight', ...) は機能する、 ただし
// pair / connect は「未実装」 で reject)。

import Capacitor
import UIKit
import AVFoundation
import AVKit

@objc(MoonlightPlugin)
public class MoonlightPlugin: CAPPlugin {

    public override func load() {
        NSLog("[MoonlightPlugin] loaded (stub during official Moonlight iOS integration)")
    }

    @objc func pair(_ call: CAPPluginCall) {
        call.reject("pair: 公式 moonlight-ios 統合中、 一時的に無効")
    }

    @objc func connect(_ call: CAPPluginCall) {
        call.reject("connect: 公式 moonlight-ios 統合中、 一時的に無効")
    }

    @objc func disconnect(_ call: CAPPluginCall) {
        call.resolve()
    }

    @objc func setVideoFrame(_ call: CAPPluginCall) {
        call.resolve()
    }

    @objc func getStatus(_ call: CAPPluginCall) {
        call.resolve(["state": "idle"])
    }

    @objc func togglePiP(_ call: CAPPluginCall) {
        call.resolve(["active": false])
    }
}
