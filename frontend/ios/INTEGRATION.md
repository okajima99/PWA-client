# Xcode 統合チェックリスト

Xcode 初期セットアップ後、 各 Phase で Xcode UI 上で必要な作業をまとめた。 CLI / 自動化スクリプトでは代替できない (= Xcode UI 必須) 操作のみ列挙。

## Phase 1 仕上げ: PWA を iOS app として実機にインストール

### 1.1 Xcode プロジェクト を開く

```bash
cd ~/repos/claude-pwa-client/frontend
npx cap open ios
```

または Finder で `~/repos/claude-pwa-client/frontend/ios/App/App.xcodeproj` を開く。

### 1.2 Native Swift / .m ファイルの Xcode プロジェクト登録

`frontend/ios/App/App/` 配下に存在する 6 ファイル:

- `MoonlightPlugin.swift`
- `MoonlightPlugin.m`
- `MoonlightBridge.swift`
- `VideoRenderer.swift`
- `AudioPlayer.swift`
- `App-Bridging-Header.h`

**自動化スクリプト (Ruby + xcodeproj gem) で `.pbxproj` に登録済**。 通常は Xcode UI 操作不要、 既に project に組み込まれてる。

確認したい場合:

```bash
cd ~/repos/claude-pwa-client/frontend
ruby ios/scripts/integrate-native-files.rb   # 何度実行しても idempotent
```

スクリプトが「既に登録済」 と出れば OK。 もし新たに Swift ファイルを追加した場合も同スクリプト再実行で取り込める。

Bridging Header (`SWIFT_OBJC_BRIDGING_HEADER = App/App-Bridging-Header.h`) も自動設定済。

### 1.3 Signing & Capabilities

1. Xcode の左ペインで **App プロジェクト** をクリック → **TARGETS > App** を選択
2. **"Signing & Capabilities"** タブ
3. **"Team"**: Apple ID (個人 free signing) を選択
4. **"Bundle Identifier"**: `com.local.claudepwaclient` (Capacitor が設定済) のまま OK
5. **"Automatically manage signing"**: ✅ ON
6. もし Signing で error が出たら "Try Again" ボタンクリック

### 1.4 Background Modes capability の確認

1. 同 "Signing & Capabilities" タブで **"+ Capability"** をクリック
2. **"Background Modes"** を追加
3. 以下にチェック:
   - ✅ Audio, AirPlay, and Picture in Picture
   - ✅ Background fetch

(Info.plist は事前更新済みなのでチェックボックスが既に ON のはず、 確認するだけ)

### 1.5 実機 build & install

1. iPhone を USB-C で接続済 + 信頼済 + デベロッパモード ON 前提
2. Xcode 上部の device セレクタで自分の iPhone を選択 (シミュレーターではなく)
3. **▶ ボタン (Run)** をクリック → build 開始 → 自動で iPhone にインストール → 起動
4. iPhone 側で初回起動時:
   - 設定 > 一般 > VPN とデバイス管理 → 自分の Apple ID 開発者証明書を**信頼**

### 1.6 動作確認

iPhone で起動した iOS app:
- ✅ チャット UI がそのまま表示される (PWA と同じ見た目)
- ✅ Tailscale 経由で backend に接続できる
- ✅ メッセージ送受信が動く
- ❌ 画面共有はまだ動かない (Phase 3-4 で実装)

ここまで動けば Phase 1 完了。

---

## Phase 3: moonlight-common-c の XCFramework を統合

### 3.1 OpenSSL for iOS の準備

moonlight-common-c は OpenSSL 必須。 iOS 用 prebuilt:

```bash
# 推奨: openssl-apple (krzyzanowskim) — Swift Package
# もしくは
git clone https://github.com/x2on/OpenSSL-for-iPhone.git ~/repos/OpenSSL-for-iPhone
cd ~/repos/OpenSSL-for-iPhone
./build-libssl.sh --version=3.2.0  # iOS arm64 + sim build
# 出力: bin/iPhoneOS/lib/libssl.a + libcrypto.a
```

OpenSSL 結果を `/opt/openssl-ios/` にシンボリックリンク or コピー。

### 3.2 moonlight-common-c の XCFramework build

```bash
cd ~/repos/claude-pwa-client/frontend
bash ios/scripts/build-moonlight-common-c.sh
```

成功すれば `ios/App/Frameworks/Moonlight.xcframework` が生成される。

### 3.3 Xcode プロジェクトに XCFramework を追加

1. Xcode プロジェクト > TARGETS > App > **"General"** タブ
2. **"Frameworks, Libraries, and Embedded Content"** セクション > **`+`**
3. **"Add Other..." > "Add Files..."**
4. `ios/App/Frameworks/Moonlight.xcframework` を選択 → "Open"
5. **Embed**: "Do Not Embed" (static library なので)

### 3.4 Bridging Header の設定

Swift から C API を呼ぶために bridging header が必要。 XCFramework が module map を提供するなら不要だが、 念のため:

1. `ios/App/App/App-Bridging-Header.h` を作成 (中身: `#import <Moonlight/Limelight.h>`)
2. Xcode > TARGETS > App > **"Build Settings"** > "Swift Compiler - General" > **"Objective-C Bridging Header"**
3. 値: `App/App-Bridging-Header.h`

### 3.5 動作確認

```swift
// MoonlightBridge.swift で:
import Foundation
// bridging header 経由で Li* 系関数が直接呼べる
let _ = LiGetEstimatedRttInfo  // コンパイル通れば link 成功
```

---

## Phase 5: PiP capability 確認

PiP は Background Modes の "Picture in Picture" を ON にする必要がある:

1. Signing & Capabilities タブ > Background Modes
2. ✅ Picture in Picture を追加でチェック
3. Phase 3.5 で実機検証

---

## トラブルシューティング

### 「Code Signing Error: Provisioning Profile Required」

→ Apple ID を Xcode > 設定 > Accounts に追加してない、 または Team 未選択。 1.3 のステップ確認

### 「Untrusted Developer」 と iPhone で出る

→ 1.5 の通り iPhone の 設定 > 一般 > VPN とデバイス管理 で自分の Apple ID 証明書を信頼

### 「Module 'Capacitor' not found」

→ `npx cap sync ios` を実行してから Xcode を再 open

### Swift ファイルが認識されない

→ 1.2 の "Add Files to App" を実行してない、 または "Add to targets: App" にチェックがなかった

### 7 日経過で「期限切れ」 表示

→ AltStore セットアップ済なら自動 refresh。 未セットアップなら Xcode から再 install (▶ ボタン)
