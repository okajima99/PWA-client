// MoonlightPlugin.m
//
// Capacitor は Swift クラスを認識するために Objective-C macro 経由で
// プラグイン情報 (名前、 公開メソッド一覧) を登録する必要がある。
// 実装は MoonlightPlugin.swift。

#import <Foundation/Foundation.h>
#import <Capacitor/Capacitor.h>

// 「web 主導アーキテクチャ」: connect は廃止、 startStream で JS から flow を制御。
// request method で client cert 付き HTTP を JS から実行可能。
// PiP / 全パソコン操作 / haptic 等は plugin method 経由で JS から呼ぶ。
CAP_PLUGIN(MoonlightPlugin, "Moonlight",
    CAP_PLUGIN_METHOD(pair, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(request, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(startStream, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(disconnect, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(setVideoFrame, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(getStatus, CAPPluginReturnPromise);
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
    CAP_PLUGIN_METHOD(setStreamViewTransform, CAPPluginReturnPromise);
)
