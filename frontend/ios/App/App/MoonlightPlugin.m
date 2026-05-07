// MoonlightPlugin.m
//
// Capacitor は Swift クラスを認識するために Objective-C macro 経由で
// プラグイン情報 (名前、 公開メソッド一覧) を登録する必要がある。
// 実装は MoonlightPlugin.swift。

#import <Foundation/Foundation.h>
#import <Capacitor/Capacitor.h>

// build 26 で web 主導アーキテクチャに変更:
// - connect は廃止、 startStream で JS から flow を制御
// - request を新設、 client cert 付き HTTP を JS から実行
// build 27: Phase 5 PiP + Phase 5.5 全操作 + Phase 6 一部 (haptic / Face ID) を一括追加
CAP_PLUGIN(MoonlightPlugin, "Moonlight",
    CAP_PLUGIN_METHOD(pair, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(request, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(startStream, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(disconnect, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(setVideoFrame, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(getStatus, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(togglePiP, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(enablePiP, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(disablePiP, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendMouseMove, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendMousePosition, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendMouseButton, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendScroll, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendKeyEvent, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendTouch, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(haptic, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(getStats, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendUtf8Text, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(sendHighResScroll, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(requestIdrFrame, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(interrupt, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(setOrientationLock, CAPPluginReturnPromise);
)
