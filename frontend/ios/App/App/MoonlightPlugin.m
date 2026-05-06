// MoonlightPlugin.m
//
// Capacitor は Swift クラスを認識するために Objective-C macro 経由で
// プラグイン情報 (名前、 公開メソッド一覧) を登録する必要がある。
// 実装は MoonlightPlugin.swift。

#import <Foundation/Foundation.h>
#import <Capacitor/Capacitor.h>

CAP_PLUGIN(MoonlightPlugin, "Moonlight",
    CAP_PLUGIN_METHOD(pair, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(connect, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(disconnect, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(setVideoFrame, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(getStatus, CAPPluginReturnPromise);
    CAP_PLUGIN_METHOD(togglePiP, CAPPluginReturnPromise);
)
