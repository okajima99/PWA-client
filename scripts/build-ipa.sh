#!/usr/bin/env bash
# build-ipa.sh — App iOS app の archive + ipa export + altstore 配置を 1 コマンド化。
#
# 経緯: 過去 archive 手順が ad-hoc で打たれてて git 管理外、 結果として 2 度ロストした
# (2026-05-15 の cache cleanup でも復元できなかった)。 再ロスト防止のため script 化。
#
# 使い方:
#   ./scripts/build-ipa.sh <build-number>
#
# 例:
#   ./scripts/build-ipa.sh 60
#
# 出力:
#   - /tmp/App-b<N>.xcarchive       (xcarchive)
#   - /tmp/App-b<N>-export/App.ipa  (export 直後の ipa)
#   - altstore/claude-pwa-client-1.0-b<N>.ipa  (配信用)
#
# 後続作業 (= 手動):
#   1. altstore/apps.json の versions[] 先頭に新 entry を追加
#      (= 説明文は変更内容に応じて書く、 size と downloadURL は本 script 出力末尾に表示)
#   2. git commit + push
#   3. iPhone の AltStore で Update タップ
#
# 環境変数:
#   TEAM_ID         signing team ID (default: <TEAM_ID>)
#   ARCHIVE_DIR     xcarchive 出力先 (default: /tmp)
#   SKIP_CAP_SYNC   1 なら cap sync をスキップ (= web の dist を更新したくない時)

set -euo pipefail

BUILD_NUM="${1:-}"
if [ -z "$BUILD_NUM" ]; then
  echo "usage: $0 <build-number>" >&2
  echo "  例: $0 60" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEAM_ID="${TEAM_ID:-<TEAM_ID>}"
ARCHIVE_DIR="${ARCHIVE_DIR:-/tmp}"
SKIP_CAP_SYNC="${SKIP_CAP_SYNC:-0}"

XCARCHIVE="$ARCHIVE_DIR/App-b${BUILD_NUM}.xcarchive"
EXPORT_DIR="$ARCHIVE_DIR/App-b${BUILD_NUM}-export"
EXPORT_OPTS="$ARCHIVE_DIR/ExportOptions-b${BUILD_NUM}.plist"
ALTSTORE_IPA="$REPO_ROOT/altstore/claude-pwa-client-1.0-b${BUILD_NUM}.ipa"

echo "==> [1/5] frontend を build"
( cd "$REPO_ROOT/frontend" && npm run build )

echo "==> [2/5] cap sync ios (= dist を ios/App/public に copy + plugins 同期)"
if [ "$SKIP_CAP_SYNC" = "1" ]; then
  echo "  (SKIP_CAP_SYNC=1 のためスキップ)"
else
  ( cd "$REPO_ROOT/frontend" && npx cap sync ios )
fi

echo "==> [3/5] project.pbxproj の CURRENT_PROJECT_VERSION を b${BUILD_NUM} に更新"
# 既に同じ値なら no-op
CURRENT=$(grep -m1 -E "CURRENT_PROJECT_VERSION = [0-9]+;" "$REPO_ROOT/frontend/ios/App/App.xcodeproj/project.pbxproj" | grep -oE "[0-9]+")
if [ "$CURRENT" = "$BUILD_NUM" ]; then
  echo "  (既に ${BUILD_NUM}、 更新不要)"
else
  sed -i '' "s/CURRENT_PROJECT_VERSION = ${CURRENT};/CURRENT_PROJECT_VERSION = ${BUILD_NUM};/g" \
    "$REPO_ROOT/frontend/ios/App/App.xcodeproj/project.pbxproj"
  echo "  ${CURRENT} -> ${BUILD_NUM}"
fi

echo "==> [4/5] xcodebuild archive (= ${XCARCHIVE})"
rm -rf "$XCARCHIVE"
( cd "$REPO_ROOT/frontend/ios/App" && \
  xcodebuild \
    -workspace App.xcworkspace \
    -scheme App \
    -configuration Release \
    -archivePath "$XCARCHIVE" \
    -destination "generic/platform=iOS" \
    archive \
    CODE_SIGN_STYLE=Automatic \
    DEVELOPMENT_TEAM="$TEAM_ID" \
    -allowProvisioningUpdates \
    > "$ARCHIVE_DIR/archive-b${BUILD_NUM}.log" 2>&1 \
)
echo "  ARCHIVE SUCCEEDED  (= ログ: $ARCHIVE_DIR/archive-b${BUILD_NUM}.log)"

echo "==> [5/5] exportArchive + altstore 配置"
# ExportOptions.plist を heredoc で生成 (= signing は automatic で provisioning profile
# は xcodebuild が必要分を自動生成する)
cat > "$EXPORT_OPTS" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>development</string>
    <key>signingStyle</key>
    <string>automatic</string>
    <key>teamID</key>
    <string>${TEAM_ID}</string>
    <key>compileBitcode</key>
    <false/>
</dict>
</plist>
EOF

rm -rf "$EXPORT_DIR"
xcodebuild \
  -exportArchive \
  -archivePath "$XCARCHIVE" \
  -exportPath "$EXPORT_DIR" \
  -exportOptionsPlist "$EXPORT_OPTS" \
  -allowProvisioningUpdates \
  > "$ARCHIVE_DIR/export-b${BUILD_NUM}.log" 2>&1
echo "  EXPORT SUCCEEDED"

cp "$EXPORT_DIR/App.ipa" "$ALTSTORE_IPA"
SIZE=$(stat -f%z "$ALTSTORE_IPA")
echo "  ipa 配置: $ALTSTORE_IPA  (= $SIZE bytes)"

echo ""
echo "==> 次の手動作業:"
echo "  1. altstore/apps.json の versions[] 先頭に新 entry を追加:"
echo "     {"
echo "       \"version\": \"1.0\","
echo "       \"buildVersion\": \"${BUILD_NUM}\","
echo "       \"date\": \"$(date -u +%Y-%m-%dT%H:%M:%S+09:00)\","
echo "       \"localizedDescription\": \"<変更内容を書く>\","
echo "       \"downloadURL\": \"https://<host>/altstore/claude-pwa-client-1.0-b${BUILD_NUM}.ipa\","
echo "       \"size\": ${SIZE},"
echo "       \"minOSVersion\": \"13.0\""
echo "     },"
echo ""
echo "  2. git add altstore/ frontend/ scripts/ && git commit -m \"feat: build ${BUILD_NUM}\""
echo "  3. iPhone の AltStore で Update タップ"
