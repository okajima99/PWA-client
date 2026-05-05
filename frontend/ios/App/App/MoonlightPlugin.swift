// MoonlightPlugin.swift
//
// Capacitor plugin that bridges JavaScript ↔ native Moonlight protocol client.
//
// JS 側 API (frontend/src/native/moonlight.ts):
//   - connect({ host, pin? }): Sunshine と pair / connect 開始
//   - disconnect(): 切断
//   - setVideoFrame({ x, y, width, height }): video display layer の位置・サイズ更新
//   - getStatus(): 状態取得
//   - togglePiP(): PiP 切替
//
// Phase 1 (今): skeleton 実装、 各メソッドはログ出力のみ。 実際の moonlight-common-c
// 連携は Phase 3 で MoonlightBridge / VideoRenderer を埋める。

import Capacitor
import UIKit
import AVFoundation
import AVKit

@objc(MoonlightPlugin)
public class MoonlightPlugin: CAPPlugin {

    // MARK: - State (Phase 3 で MoonlightBridge / VideoRenderer に置き換え)

    private enum ConnectionState: String {
        case idle
        case pairing
        case connecting
        case connected
        case failed
    }

    private var state: ConnectionState = .idle
    private var displayLayer: AVSampleBufferDisplayLayer?
    private var pipController: AVPictureInPictureController?

    // MARK: - Plugin lifecycle

    public override func load() {
        // Web view から呼ばれる前の初期化。
        // Phase 3 で AVSampleBufferDisplayLayer を viewController.view に attach する。
        NSLog("[MoonlightPlugin] loaded")
    }

    // MARK: - Public API (JS から呼べる)

    /// Sunshine に接続する。 初回は PIN ペアリング、 以降は保存済キーで自動接続。
    @objc func connect(_ call: CAPPluginCall) {
        let host = call.getString("host") ?? ""
        let pin = call.getString("pin")
        guard !host.isEmpty else {
            call.reject("host is required")
            return
        }
        NSLog("[MoonlightPlugin] connect host=\(host) pin=\(pin ?? "nil")")

        // TODO Phase 3: MoonlightBridge.connect(host:, pin:) を呼ぶ
        // 現状は state を connecting に変えて即 connected を演じるダミー
        state = .connecting
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            self.state = .connected
            self.notifyListeners("statusChange", data: ["state": self.state.rawValue])
            call.resolve([
                "paired": true,
                "needsPin": false,
            ])
        }
    }

    /// Sunshine からの切断。 video / audio stream を停止。
    @objc func disconnect(_ call: CAPPluginCall) {
        NSLog("[MoonlightPlugin] disconnect")
        // TODO Phase 3: MoonlightBridge.disconnect() を呼ぶ
        state = .idle
        notifyListeners("statusChange", data: ["state": state.rawValue])
        call.resolve()
    }

    /// JS 側の <div> の位置 / サイズに合わせて native video layer を配置。
    /// PWA Phase 6 までは <div id="desktop-video-slot"> の getBoundingClientRect を渡す前提。
    @objc func setVideoFrame(_ call: CAPPluginCall) {
        let x = call.getDouble("x") ?? 0
        let y = call.getDouble("y") ?? 0
        let w = call.getDouble("width") ?? 0
        let h = call.getDouble("height") ?? 0

        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            // TODO Phase 4: displayLayer.frame = CGRect(x:y:width:height:)
            NSLog("[MoonlightPlugin] setVideoFrame x=\(x) y=\(y) w=\(w) h=\(h)")
            self.displayLayer?.frame = CGRect(x: x, y: y, width: w, height: h)
        }
        call.resolve()
    }

    /// 状態 + 統計情報を返す。 stream 中の fps / bitrate / RTT も含む。
    @objc func getStatus(_ call: CAPPluginCall) {
        var data: [String: Any] = ["state": state.rawValue]
        // TODO Phase 3: MoonlightBridge から stats 取得して埋める
        // data["fps"] = ...
        // data["bitrate_kbps"] = ...
        // data["rtt_ms"] = ...
        // data["codec"] = "h264" / "hevc"
        call.resolve(data)
    }

    /// PiP の切替。 AVPictureInPictureController を on/off。
    @objc func togglePiP(_ call: CAPPluginCall) {
        DispatchQueue.main.async { [weak self] in
            guard let self = self, let pip = self.pipController else {
                call.resolve(["active": false])
                return
            }
            if pip.isPictureInPictureActive {
                pip.stopPictureInPicture()
                call.resolve(["active": false])
            } else {
                pip.startPictureInPicture()
                call.resolve(["active": true])
            }
        }
    }
}
