//
// App-Bridging-Header.h
//
// Swift から moonlight-common-c (C library) の API を直接呼ぶための bridging header。
// Phase 3 で `Moonlight.xcframework` を Xcode project に追加した後、 ここに
// `#import <Moonlight/Limelight.h>` を有効化する。
//
// Xcode 側設定:
//   TARGETS > App > Build Settings > Swift Compiler - General
//     > "Objective-C Bridging Header" = "App/App-Bridging-Header.h"
//
// 詳細は ios/INTEGRATION.md の Phase 3.4 参照。

#ifndef App_Bridging_Header_h
#define App_Bridging_Header_h

// moonlight-common-c (XCFramework 経由)
#import "Limelight.h"

// SDL2: AppDelegate で SDL_SetMainReady() を呼ぶため bridging。 SDL_MAIN_HANDLED は
// SDL の main ハイジャックを無効化、 = アプリ自身が UIApplicationMain を呼ぶ前提。
// 公式 moonlight-ios の main.m と同じパターン。
#define SDL_MAIN_HANDLED
#import <SDL.h>

// Phase 3 (= 公式 moonlight-ios の品質コア統合):
// Connection / VideoDecoderRenderer / StreamConfiguration / Utils / Logger を
// Swift 側 (MoonlightPlugin / MoonlightBridge) から直接呼べるよう expose する。
#import "Connection.h"
#import "VideoDecoderRenderer.h"
#import "StreamConfiguration.h"
#import "ConnectionCallbacks.h"
#import "Utils.h"
#import "Logger.h"

#endif /* App_Bridging_Header_h */
