# Claude PWA Client

Claude Code をスマートフォンから操作するための PWA クライアント。 Mac で動かしてる
バックエンドに Tailscale 経由で iPhone / Android のブラウザから繋ぎ、 ホーム画面に
追加すれば普通のチャットアプリのように使える。

## できること

- **チャット**: 複数のセッション (= 議題) を並走、 タブで切替。 SSE で逐次表示
- **バックグラウンド継続**: 画面閉じても Mac 側で処理継続、 復帰時に自動再接続して
  バッファ受信
- **Web Push 通知**: AskUserQuestion 等で proactive に iOS / Android に通知
- **Proactive 自動配信**: Monitor / cron / ScheduleWakeup 等で agent が自発した turn を
  持続 SSE で即時表示 (= 「最新を取得」 を押さなくても自動で流れる)
- **Model & Effort 切替**: ⋯ メニュー → Model & Effort ダイアログで session 単位に
  model (opus / sonnet / haiku) × effort (low / medium / high / xhigh / max) を変更
- **通知センター自動掃除**: PWA を開く / フォアグラウンド復帰のタイミングで OS 通知
  センター + アプリバッジ + backend 未読カウンタを 3 点同期掃除
- **ファイルプレビュー**: チャット内のパスをタップ → Markdown / シンタックスハイライト
- **ファイルツリー**: サーバ上のディレクトリをパネルで閲覧
- **画像 / テキスト添付**: マルチパートで送信、 履歴に永続化
- **ステータスバー**: 使用モデル / 5h usage / 7d usage / context 使用率 をリアルタイム表示
- **メッセージ履歴永続化**: lz-string 圧縮で localStorage に保存

### 追加機能 (任意セットアップ)

- **デスクトップ画面共有**: PWA 内に [Sunshine](https://github.com/LizardByte/Sunshine)
  + [moonlight-web-stream](https://github.com/MrCreativ3001/moonlight-web-stream) 経由で
  デスクトップを映す。 タッチで遠隔操作。 Sunshine は **Windows / Linux / macOS** で動くので
  ホスト OS は問わない (= 動作確認は macOS、 他は手順を読み替えれば動くはず)。 セットアップ手順は後述

## アーキテクチャ

```
[スマートフォン]                  [Mac (= 開発機)]
                                ┌──────────────────────┐
   PWA (Safari/Chrome) ─────┐   │ FastAPI backend      │
       │                    │   │   ├ Claude Code CLI  │
       │                    ├─▶ │   │   subprocess     │
       │                    │   │   └ Web Push (VAPID) │
   ホーム画面追加で             │   │                      │
   standalone 起動           │   │ moonlight-web-stream │ ← 任意
                            │   │   └ Sunshine         │ ← 任意
                            │   └──────────────────────┘
                            │              ↕ Tailscale
                            └──────────────┘
```

- バックエンドは Mac 上で常駐、 Claude Code CLI を subprocess として呼び出す
- iPhone / Android からは Tailscale 経由で Mac の HTTPS にアクセス
- インターネット公開はしない、 Tailscale tailnet 内のみ

## セットアップ

2 段階の構成。 **Path A** はチャット + 通知だけのシンプル版、 **Path B** はそれに加えて
デスクトップ画面共有まで使う上位版。

### Path A: チャット + 通知

必要なもの:
- Mac (= バックエンドホスト)
- Python 3.11+ (conda 推奨)
- Node.js (フロントエンドビルド用)
- Tailscale (Mac + スマホ両方に install + 同一 tailnet)
- Claude Code CLI (`claude` コマンド、 認証済み)

#### バックエンド

```bash
git clone https://github.com/<your-handle>/claude-pwa-client.git
cd claude-pwa-client

# Python 環境
conda create -n pwa-client python=3.11
conda activate pwa-client
pip install -r backend/requirements.txt

# 設定ファイル
cp backend/config.example.json backend/config.json
# config.json を編集してエージェントの cwd / claude コマンドパス等を設定

# Web Push 用の VAPID 鍵生成 (1 度だけ)
python backend/gen_vapid.py  # backend/vapid.json を生成

# 起動
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

#### フロントエンド

```bash
cd frontend
npm install
npm run build  # dist/ を生成、 バックエンドが配信
```

#### Tailscale で外部公開 (= tailnet 内のみ)

```bash
# backend を tailnet 経由で HTTPS 提供。 同一オリジン (= /) に張る。
tailscale serve --bg http://localhost:8000
```

これで `https://<your-host>.tail<xxxx>.ts.net/` が backend を指す状態になる。
`tailscale serve status` で確認可能。

#### backend を auto-start にする (= macOS 例)

毎回 `uvicorn` を手で起動するのは面倒なので、 macOS なら LaunchAgent で常駐させる。
`~/Library/LaunchAgents/com.example.claudepwa.plist` 例:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.claudepwa</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd /path/to/claude-pwa-client && source /path/to/miniforge/etc/profile.d/conda.sh && conda activate pwa-client && exec uvicorn backend.main:app --host 0.0.0.0 --port 8000</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/path/to/claude-pwa-client/logs/backend.out</string>
  <key>StandardErrorPath</key><string>/path/to/claude-pwa-client/logs/backend.err</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.claudepwa.plist
```

backend は app 内で `RotatingFileHandler` を組んでるので、 上の `StandardOutPath` は
最低限の補助 (= uvicorn 起動行・致命例外用)。 メイン log は `logs/backend.access.log`
/ `logs/backend.error.log` に 5MB × 3 世代で自動 rotate される (= 長期運用しても
ディスクを食い続けない)。

Windows なら NSSM や Task Scheduler、 Linux なら systemd user service で同等のことを。

#### スマホから接続

1. Tailscale で Mac の MagicDNS 名を確認 (例: `your-host.tail<xxxx>.ts.net`)
2. スマホで `https://<your-host>.tail<xxxx>.ts.net/` を開く
3. iOS Safari なら 共有 → ホーム画面に追加 で PWA 化
4. 通知を有効にしたい場合は ⋯ メニューから「通知を有効にする」 (iOS は 16.4+ +
   ホーム画面追加が必須)

### Path B: デスクトップ画面共有も追加 (= 開発者向けオプション)

> ⚠️ **optional / 開発者向け**。 Rust nightly + 30 分の自前ビルドが要るので、
> 「自分の PC の画面を自分のスマホで遠隔操作したい」 という明確な用途がある人だけ
> 進めてください。 chat + 通知だけ使う人は Path A で完結します。
>
> ホスト OS は **Windows / Linux / macOS** どれでも OK (= Sunshine は cross-platform)。
> 以下は **macOS の例**、 他 OS は同等の手段で読み替えて (= brew → apt/scoop 等、
> 権限設定 → 各 OS の screen capture 許可)。 動作確認は macOS で実施。

Path A の構成に加えて、 Sunshine + moonlight-web-stream をホスト機に install すると
PWA 内の 🖥 ボタンからデスクトップ画面共有 + タッチ遠隔操作が動く。

#### Sunshine (画面キャプチャ + Moonlight protocol サーバ)

```bash
# macOS の例: Homebrew で install (Windows は scoop / installer、 Linux は apt/rpm/AUR)
brew tap LizardByte/homebrew
brew install sunshine-beta

# 初回起動して config UI でユーザ作成 + アプリ登録 (= "Desktop" がデフォルトで入る)
sunshine
# ブラウザで https://localhost:47990 → 管理者アカウント作成
```

ホスト OS 側で **画面キャプチャ + 入力注入の許可**を sunshine に与える:
- macOS: System Settings → プライバシーとセキュリティ で **画面録画**と**入力監視 (Input
  Monitoring) / アクセシビリティ (Accessibility)** の両方に sunshine を追加 (= 後者は
  ブラウザからのタップ / キー入力を Mac に注入するために必須、 忘れると画面は映るが
  クリックが効かない)
- Windows: 通常不要 (= UAC レベルで実行)
- Linux: X11 / Wayland の捕捉設定 (= Sunshine docs 参照)

自動起動の例 (macOS LaunchAgent、 `~/Library/LaunchAgents/dev.lizardbyte.sunshine.plist`):

```xml
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.lizardbyte.sunshine</string>
  <key>ProgramArguments</key>
  <array><string>/opt/homebrew/bin/sunshine</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
```

`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.lizardbyte.sunshine.plist` で
有効化。 Linux は systemd user service、 Windows はサービス登録で同等。

> ⚠️ **sunshine の encoder hang 注意**: `launchctl kickstart -k` (= SIGTERM) で再起動
> すると、 macOS の ScreenCaptureKit / VideoToolbox の resource が graceful shutdown
> 中に中途半端解放されて、 respawn 後の encoder 初期化で永久待ちすることがある。
> 復旧は `kill -9 <sunshine pid>` で SIGKILL → KeepAlive が 10 秒で respawn → clean
> state で起動。 Mac 再起動経由なら問題なし。

#### moonlight-web-stream (= Sunshine ↔ ブラウザ WebRTC bridge)

公式 release が無い OS は Rust から自前ビルド (= macOS 含む)。

```bash
# Rust nightly install (= macOS の例、 他 OS は rustup の公式手順)
brew install rustup
rustup default nightly

# clone + build (= 全 OS 共通、 cargo / npm が要る)
git clone --recurse-submodules https://github.com/MrCreativ3001/moonlight-web-stream.git
cd moonlight-web-stream
cargo build --release   # 30 分くらい
npm install
npm run build
cp -r dist static  # release mode は static/ を見る

# 起動
./target/release/web-server
```

**config** (`server/config.json`) の `web_server` セクションで:

```json
{
  "web_server": {
    "url_path_prefix": "/moonlight",
    "default_user_id": <ペアリング後に決まる user_id>
  }
}
```

- `url_path_prefix = /moonlight` で Tailscale Serve の `/moonlight` プロキシと整合
- `default_user_id` を設定すると PWA の iframe が認証スキップで即起動 (= 友達に
  URL 渡すだけで動く)

**自動起動** (macOS LaunchAgent 例、 `~/Library/LaunchAgents/com.example.moonlight-web-stream.plist`):

```xml
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.moonlight-web-stream</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/moonlight-web-stream/target/release/web-server</string></array>
  <key>WorkingDirectory</key><string>/path/to/moonlight-web-stream</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

**ペアリング** (= 1 度だけ):

1. ブラウザで `http://localhost:8080/` を開いてユーザ作成
2. Hosts に localhost を追加 → Pair → PIN が表示される
3. Sunshine の Web UI (= `https://localhost:47990`) → PIN タブで PIN 入力 → Send
4. moonlight-web-stream 側で「Paired」 表示になれば完了

> ⚠️ **pair が壊れた時の復旧**: ホスト再起動の挙動で moonlight-web-stream の
> `data.json` の pair_info と sunshine の `named_devices` の cert が一致しなくなる
> ことがある。 その時は data.json の hosts エントリを空にして moonlight-web-stream を
> 再起動 → PWA から Add Host → Pair → sunshine admin で PIN 入力、 の流れで作り直す。

#### Tailscale Serve で同一オリジン公開

PWA から `/moonlight/` 配下にプロキシで届くよう Tailscale Serve を設定 (= Path A で
既に backend を `/` に張ってる前提、 追加で `/moonlight` を mount):

```bash
tailscale serve --bg --set-path=/moonlight http://localhost:8080/moonlight
```

これで同一オリジン (= `https://<your-host>.tail<xxxx>.ts.net/moonlight/...`) で
moonlight-web-stream が叩けて、 PWA の iframe / CORS / Cookie 制約を全部素通りできる。

#### 音声を PWA に乗せる (= 任意、 macOS 例)

Sunshine がキャプチャできる audio sink を別途用意する。 macOS は通常出力を直接
sunshine に渡せないので、 [BlackHole](https://github.com/ExistentialAudio/BlackHole)
等の仮想 audio device を経由:

```bash
brew install blackhole-2ch
```

`~/.config/sunshine/sunshine.conf` に:

```
audio_sink = BlackHole 2ch
```

ただこのままだと Mac 本体スピーカーに音が出なくなる (= 出力先が BlackHole に固定)。
「PWA で見てる時だけ BlackHole に切替、 接続切れたら元のスピーカーに戻す」 を
LaunchAgent + `switchaudio-osx` の常駐スクリプトで自動化できる:

```bash
brew install switchaudio-osx
```

`~/Library/Application Support/sunshine-audio-switch/switch.sh` (= 抜粋):

```bash
#!/bin/bash
LOG="$HOME/.config/sunshine/sunshine.log"
TARGET="BlackHole 2ch"
STATE="/tmp/sunshine-audio-prev"
SWITCH="/opt/homebrew/bin/SwitchAudioSource"
tail -n0 -F "$LOG" | while IFS= read -r line; do
  case "$line" in
    *"New streaming session started"*)
      PREV=$("$SWITCH" -c); [ "$PREV" != "$TARGET" ] && { printf '%s' "$PREV" > "$STATE"; "$SWITCH" -s "$TARGET"; } ;;
    *"CLIENT DISCONNECTED"*)
      [ -f "$STATE" ] && { "$SWITCH" -s "$(cat $STATE)"; rm -f "$STATE"; } ;;
  esac
done
```

LaunchAgent 化 (= `com.example.sunshine-audio-switch.plist`) で常駐させる。

Windows / Linux は OS の loopback audio で直接拾える場合が多く、 仮想 device 不要な
ことが多い (= Sunshine docs の OS 別注記を参照)。

## Troubleshooting

### Chrome / Brave で `NET::ERR_CERTIFICATE_TRANSPARENCY_REQUIRED` 等の HTTPS 証明書エラー

Tailscale が発行する Let's Encrypt 証明書まわりで Chromium 系ブラウザが拒否する
ケースが Tailscale 側の既知 issue として残ってる (=
[tailscale/tailscale#16179](https://github.com/tailscale/tailscale/issues/16179))。
順に試す:

1. **incognito / private window で開き直す** (= 過去 cert state を抜く、 上記 issue で
   workaround として有効報告あり)
2. **Tailscale 管理画面で HTTPS Certificates が ON か確認**
   ([docs](https://tailscale.com/docs/how-to/set-up-https-certificates))
3. **OS の時刻が正しいか確認** (= 大きくズレてると CT 検証失敗)

上記で抜けない、 もしくは「とりあえず動かしたい」 場合は **direct IP HTTP fallback** で
回避できる:

```
http://<your-tailscale-ip>:8000
```

- `<your-tailscale-ip>` は Tailscale 管理画面 or `tailscale ip` で確認 (= `100.x.x.x`)
- tailnet 内の通信は WireGuard で暗号化済なので、 HTTPS を剥がしても tailnet 内なら実害なし
- ホーム画面追加 (= PWA 化) も HTTP URL のままで可能

## 設定ファイル

### `backend/config.json`

```json
{
  "agents": {
    "session_default": {
      "cwd": "/path/to/workdir",
      "model": "Opus"
    }
  },
  "claude_path": "/path/to/claude",
  "rate_limits_log": "/path/to/rate-limits.jsonl",
  "notification_title": "Claude",
  "cors_allow_origins": []
}
```

- `claude_path`: `claude` コマンドのフルパス (`which claude` で確認)。 未指定だと
  SDK が PATH から拾うが、 conda 等の環境差で読めない場合は明示する
- 各エージェントの `cwd` に置かれた `CLAUDE.md` が `claude` コマンド起動時に自動 load
  される
- `cors_allow_origins`: 通常は `[]` (= backend が同一オリジンで frontend を配信するので
  CORS 不要)。 Vite dev server から叩く場合は `["http://localhost:5173"]` 等を入れる

### `frontend/.env` / `frontend/.env.local`

- **`frontend/.env`** はリポに commit される既定値 (アプリ名 / アイコン等)
- **`frontend/.env.local`** は gitignore 済の個人用 override。 例:

```
VITE_API_BASE=https://<your-host>.tail<xxxx>.ts.net
```

`VITE_API_BASE` 未設定なら同一オリジン相対 URL になる (= backend が frontend を配信する
標準構成なら不要)。 backend と frontend を別オリジンで動かす場合だけ設定。

## ディレクトリ構成

```
claude-pwa-client/
├── backend/                 # FastAPI バックエンド (Python)
│   ├── main.py              # エントリポイント + ルータ集約
│   ├── chat_routes.py       # /chat/stream, /reconnect, /stop, /end
│   ├── files_routes.py      # /file, /files/tree
│   ├── proxy_routes.py      # /proxy/... (Anthropic API リバプロ)
│   ├── push.py              # Web Push + 通知履歴 + SSE listener
│   ├── sdk_runner.py        # Claude Code CLI subprocess 管理
│   ├── session_logging.py   # セッション単位ログ
│   ├── config.example.json
│   └── requirements.txt
└── frontend/                # React + Vite
    ├── src/
    │   ├── App.jsx          # メイン (= タブ / チャット / status / 画面共有トグル)
    │   ├── components/
    │   │   ├── MoonlightFrame.jsx   # 画面共有 iframe
    │   │   ├── SessionDrawer.jsx    # 会話一覧ドロワー
    │   │   ├── StatusBar.jsx        # 使用率表示
    │   │   ├── MessageItem.jsx      # 1 メッセージ render
    │   │   └── ...
    │   ├── hooks/                   # チャット / SSE / 永続化 hook 群
    │   └── utils/
    └── public/
        ├── manifest.template.json   # PWA manifest (env で値注入)
        └── sw.js                    # Service Worker (Web Push 受信)
```

## ライセンス

Apache License 2.0 (`LICENSE` + `NOTICE` 参照)。

Sunshine / moonlight-web-stream は GPL-3.0 だが、 本リポはそれらを**バンドル / リンク
していない** — 別プロセスとして起動し、 HTTP / WebRTC 経由で連携するだけなので、
このリポ自体には GPL の copyleft は波及しない (FSF GPL FAQ「プロセス分離は通常
derivative work ではない」 に依拠)。

派生物は `NOTICE` を保持し、 改造した主要ファイルにその旨を明記すること
(Apache-2.0 §4)。

## 謝辞

- [Claude Code](https://docs.claude.com/en/docs/claude-code) — Anthropic 公式 CLI
- [Sunshine](https://github.com/LizardByte/Sunshine) — 自己ホスト型ゲームストリームサーバ
- [moonlight-web-stream](https://github.com/MrCreativ3001/moonlight-web-stream) — Sunshine をブラウザ WebRTC で受ける bridge
