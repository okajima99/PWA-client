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

// Phase 3 で有効化:
// #import <Moonlight/Limelight.h>

#endif /* App_Bridging_Header_h */
