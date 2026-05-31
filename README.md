# Claude PWA Client

Claude Code をスマートフォンから操作するための PWA クライアント。 ホストマシン上で動かす
バックエンドに Tailscale 経由で iPhone / Android のブラウザから接続し、 ホーム画面に追加して
スタンドアロン PWA として利用する。

## 主な機能

- **チャット**: 複数のセッションを並走させ、 タブで切り替え。 SSE で逐次表示
- **バックグラウンド継続**: 画面を閉じてもホスト側で処理が継続し、 復帰時に自動再接続して
  差分を受信
- **Web Push 通知**: `AskUserQuestion` 等のプロアクティブな問い合わせを iOS / Android に通知
- **Proactive 自動配信**: `Monitor` / `cron` / `ScheduleWakeup` 等で agent が自発した turn を
  持続 SSE で即時表示する
- **通知センター自動同期**: PWA を開く / フォアグラウンド復帰のタイミングで OS 通知センター ・
  アプリバッジ ・ backend 未読カウンタを同期掃除
- **ファイルプレビュー**: チャット内のパスをタップして Markdown ・ シンタックスハイライト表示
- **ファイルツリー**: サーバ上のディレクトリをパネルで閲覧
- **画像 / テキスト添付**: マルチパートで送信し、 履歴に永続化
- **ステータスバー**: 使用モデル ・ 5h usage ・ 7d usage ・ context 使用率をリアルタイム表示
- **メッセージ履歴永続化**: lz-string で圧縮して localStorage に保存

### 追加機能 (任意セットアップ)

- **デスクトップ画面共有**: [Sunshine](https://github.com/LizardByte/Sunshine) +
  [moonlight-web-stream](https://github.com/MrCreativ3001/moonlight-web-stream) を経由して
  ホスト機のデスクトップを PWA 内で映し、 タッチで遠隔操作する。 Sunshine は Windows /
  Linux / macOS で動作する (本リポでの動作確認は macOS)。 セットアップは後述

## アーキテクチャ

```
[スマートフォン]                  [ホスト機]
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

- backend はホスト機で常駐し、 `claude` CLI を **実 PTY (疑似端末) + tmux** で起動する。
  ターミナルで `claude` を起動する時とまったく同じ TUI 形式の経路を取り、 SDK や
  `--print` 等の非対話モードは使わない。 出力は claude が書く会話ログ (JSONL) を tail
  して SSE でチャット UI に配信し、 入力は tmux 経由でセッションへ送出する
  (出力 = JSONL tail / 入力 = キー送出 の分離設計)
- 手元の Claude Code サブスクリプション (Pro / Max) でそのまま動作する。 別途の API キー
  や従量課金は不要 (`claude` CLI 自身の認証経路をそのまま利用)
- スマートフォンからは Tailscale 経由でホスト機の HTTPS にアクセスする
- インターネット公開はせず、 Tailscale tailnet 内のみで疎通する

## セットアップ

2 段階構成。 **Path A** はチャット + 通知のみのシンプル版、 **Path B** はそれに加えて
デスクトップ画面共有まで含む上位版。

### Path A: チャット + 通知

必要なもの:

- ホスト機 (macOS / Linux。 Windows の場合は WSL2 経由。 後述の **Windows (WSL2)** 節参照)
- Python 3.11+ (conda 推奨)
- Node.js (フロントエンドビルド用)
- Tailscale (ホスト機とスマートフォン両方にインストールし、 同一 tailnet に参加させる)
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

#### Tailscale で tailnet 内に公開

```bash
# backend を tailnet 経由で HTTPS 提供 (同一オリジンで /)
tailscale serve --bg http://localhost:8000
```

これで `https://<your-host>.tail<xxxx>.ts.net/` が backend を指す。 `tailscale serve status`
で接続状態を確認できる。

#### backend を常駐起動する (macOS LaunchAgent)

`uvicorn` を毎回手動起動する代わりに、 macOS なら LaunchAgent で常駐させる。
`~/Library/LaunchAgents/com.example.claudepwa.plist`:

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

backend はアプリ内で `RotatingFileHandler` を構成しているため、 上記の `StandardOutPath`
は `uvicorn` 起動行および致命例外を拾う補助用。 メインログは `logs/backend.access.log` /
`logs/backend.error.log` に 5 MB × 3 世代で自動ローテートされる。

Linux では systemd user service で同等の常駐構成を取れる。 Windows は後述の
**Windows (WSL2)** 節を参照。

#### Windows (WSL2)

backend は POSIX 前提の機能 (PTY / tmux / lsof) を利用するため Windows ネイティブでは
動作しない。 Windows で利用する場合は WSL2 (Ubuntu) の中で Linux 版 backend を動かす。
frontend は Windows 側のブラウザから Tailscale 経由でそのままアクセスできる。

1. **WSL2 のインストール** (PowerShell を管理者で起動):
   ```powershell
   wsl --install -d Ubuntu
   ```
   再起動後に Ubuntu が立ち上がるのでユーザを作成する。

2. **Ubuntu 内で依存をインストール** (macOS 手順と同等、 `brew` の代わりに `apt`):
   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip nodejs npm tmux git curl
   # claude CLI のインストールは公式手順に従う:
   # https://docs.claude.com/en/docs/claude-code
   ```

3. **リポジトリと backend / frontend のセットアップ** (Path A と同手順):
   ```bash
   git clone https://github.com/<your-handle>/claude-pwa-client.git
   cd claude-pwa-client
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r backend/requirements.txt
   cp backend/config.example.json backend/config.json
   # config.json の claude_path は `which claude` の結果と揃える
   python backend/gen_vapid.py
   (cd frontend && npm install && npm run build)
   ```

4. **systemd user service で常駐起動**
   (`~/.config/systemd/user/claudepwa.service`):
   ```ini
   [Unit]
   Description=Claude PWA backend

   [Service]
   WorkingDirectory=%h/claude-pwa-client
   ExecStart=/bin/bash -lc 'source .venv/bin/activate && exec uvicorn backend.main:app --host 0.0.0.0 --port 8000'
   Restart=always

   [Install]
   WantedBy=default.target
   ```
   有効化:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now claudepwa.service
   # Ubuntu シェルを閉じた後も backend を継続させる:
   loginctl enable-linger $USER
   ```

5. **Tailscale を WSL2 内にインストール** (WSL を Linux ホストとして tailnet に参加させる):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   sudo tailscale serve --bg http://localhost:8000
   ```
   これで `https://<wsl-host>.tail<xxxx>.ts.net/` が backend を指す。 Windows 側にも
   Tailscale をインストールして同一 tailnet に参加させれば、 Windows のブラウザ・他端末
   からも疎通する。

   参考: WSL2 のネットワークモードを `mirrored` に設定すれば Windows ホストの Tailscale を
   共有することも可能だが、 設定が増えるため WSL 内に直接 Tailscale を入れる構成の方が
   簡素かつ安定する。

#### スマートフォンから接続

1. Tailscale でホスト機の MagicDNS 名を確認する (例: `your-host.tail<xxxx>.ts.net`)
2. スマートフォンで `https://<your-host>.tail<xxxx>.ts.net/` を開く
3. iOS Safari の場合は 共有 → ホーム画面に追加 で PWA 化する
4. 通知を有効化する場合は ⋯ メニューの「通知を有効にする」を選択する
   (iOS 16.4+ かつホーム画面追加済みであることが必須)

### Path B: デスクトップ画面共有 (任意)

> このセクションは Rust nightly での自前ビルドが必要なため、 デスクトップ画面共有が
> 不要であれば Path A だけで完結する。
>
> Sunshine は Windows / Linux / macOS で動作するためホスト OS は問わない。 以下の例は
> macOS をベースに記述しており、 他 OS では同等のパッケージマネージャ / 権限設定に
> 読み替える (本リポでの動作確認は macOS)。

Path A の構成に Sunshine + moonlight-web-stream を追加すると、 PWA の 🖥 ボタンから
ホスト機のデスクトップ画面共有とタッチによる遠隔操作が利用できる。

#### Sunshine (画面キャプチャ + Moonlight プロトコルサーバ)

```bash
# macOS の例 (Windows は scoop / インストーラ、 Linux は apt / rpm / AUR を利用):
brew tap LizardByte/homebrew
brew install sunshine-beta

# 初回起動して config UI でユーザ作成 + アプリ登録 ("Desktop" がデフォルトで入る)
sunshine
# ブラウザで https://localhost:47990 → 管理者アカウント作成
```

ホスト OS 側で画面キャプチャと入力注入の許可を Sunshine に与える:

- **macOS**: System Settings → プライバシーとセキュリティ で「画面録画」と「入力監視 /
  アクセシビリティ」の両方に Sunshine を追加する。 後者はブラウザからのタップ ・ キー入力
  をホストに注入するために必須で、 未設定の場合は画面は映るが操作が効かない
- **Windows**: 通常は追加設定不要 (UAC レベルで実行される)
- **Linux**: X11 / Wayland のキャプチャ設定が必要 (Sunshine 公式ドキュメント参照)

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
有効化する。 Linux は systemd user service、 Windows はサービス登録で同等の常駐構成が
取れる。

> **Note (macOS) — Sunshine の encoder hang 対策**: `launchctl kickstart -k` (SIGTERM)
> での再起動時に、 ScreenCaptureKit / VideoToolbox のリソースが graceful shutdown 中に
> 中途解放され、 respawn 後の encoder 初期化でハングする事例がある。 復旧手順は
> `kill -9 <sunshine pid>` で SIGKILL → KeepAlive による 10 秒後の respawn でクリーン
> な状態で起動する。 OS 再起動経由では発生しない。

#### moonlight-web-stream (Sunshine ↔ ブラウザの WebRTC ブリッジ)

公式リリースが無い OS では Rust から自前ビルドする。

```bash
# Rust nightly のインストール (macOS の例。 他 OS は rustup 公式手順)
brew install rustup
rustup default nightly

# clone + build (cargo / npm が必要)
git clone --recurse-submodules https://github.com/MrCreativ3001/moonlight-web-stream.git
cd moonlight-web-stream
cargo build --release
npm install
npm run build
cp -r dist static   # release mode は static/ を参照する

# 起動
./target/release/web-server
```

`server/config.json` の `web_server` セクション:

```json
{
  "web_server": {
    "url_path_prefix": "/moonlight",
    "default_user_id": <ペアリング後に決まる user_id>
  }
}
```

- `url_path_prefix = /moonlight` で Tailscale Serve の `/moonlight` プロキシと整合させる
- `default_user_id` を設定すると PWA の iframe が認証なしで起動できる (URL 共有のみで
  接続可能)

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

**ペアリング** (初回のみ):

1. ブラウザで `http://localhost:8080/` を開きユーザを作成する
2. Hosts に localhost を追加 → Pair で PIN が表示される
3. Sunshine Web UI (`https://localhost:47990`) → PIN タブで上記 PIN を入力 → Send
4. moonlight-web-stream 側で「Paired」表示になれば完了

> **Note — ペアリングが壊れた場合の復旧**: ホスト再起動の挙動で moonlight-web-stream の
> `data.json` 内 `pair_info` と Sunshine の `named_devices` 内 cert が不整合になる場合
> がある。 復旧手順は data.json の hosts エントリを空にして moonlight-web-stream を
> 再起動し、 PWA から Add Host → Pair → Sunshine admin で PIN 入力、 の流れで再構築する。

#### Tailscale Serve で同一オリジン公開

PWA から `/moonlight/` 配下にリバースプロキシで届かせるため Tailscale Serve を設定する
(Path A で backend を `/` に提供済みの前提、 追加で `/moonlight` をマウント):

```bash
tailscale serve --bg --set-path=/moonlight http://localhost:8080/moonlight
```

これで `https://<your-host>.tail<xxxx>.ts.net/moonlight/...` の同一オリジンで
moonlight-web-stream にアクセスでき、 PWA の iframe / CORS / Cookie 制約を回避できる。

#### 音声を PWA に流す (任意、 macOS 例)

Sunshine がキャプチャできる audio sink を別途用意する。 macOS では通常出力を直接 Sunshine
に渡せないため、 [BlackHole](https://github.com/ExistentialAudio/BlackHole) 等の仮想
オーディオデバイスを経由する:

```bash
brew install blackhole-2ch
```

`~/.config/sunshine/sunshine.conf` に:

```
audio_sink = BlackHole 2ch
```

この設定のままだとホスト本体のスピーカーから音が出なくなる (出力先が BlackHole に固定
されるため)。 「PWA 接続中だけ BlackHole に切り替え、 接続終了時に元の出力に戻す」 を
LaunchAgent と `switchaudio-osx` を用いた常駐スクリプトで自動化できる:

```bash
brew install switchaudio-osx
```

`~/Library/Application Support/sunshine-audio-switch/switch.sh` (抜粋):

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

これを `com.example.sunshine-audio-switch.plist` として LaunchAgent 化し常駐させる。

Windows / Linux では OS のループバックオーディオで直接キャプチャできる場合が多く、 仮想
デバイスが不要なケースが多い (Sunshine 公式ドキュメントの OS 別注記を参照)。

## Troubleshooting

### Chromium 系ブラウザで HTTPS 証明書エラー (`NET::ERR_CERTIFICATE_TRANSPARENCY_REQUIRED` 等)

Tailscale が発行する Let's Encrypt 証明書周辺で Chromium 系ブラウザが拒否するケースが
Tailscale 側の既知 issue として残存している
([tailscale/tailscale#16179](https://github.com/tailscale/tailscale/issues/16179))。
以下を順に試す:

1. **シークレット / プライベートウィンドウで開き直す** (過去の cert state を回避する。
   上記 issue で workaround として有効報告あり)
2. **Tailscale 管理画面で HTTPS Certificates が有効か確認する**
   ([Tailscale docs](https://tailscale.com/docs/how-to/set-up-https-certificates))
3. **OS の時刻が正しいか確認する** (時刻ズレが大きいと CT 検証に失敗する)

上記で解決しない場合、 direct IP の HTTP フォールバックで暫定回避できる:

```
http://<your-tailscale-ip>:8000
```

- `<your-tailscale-ip>` は Tailscale 管理画面または `tailscale ip` で確認できる (`100.x.x.x`)
- tailnet 内の通信は WireGuard で暗号化されているため、 HTTPS を剥がしても tailnet 内では
  実害が出ない
- HTTP URL のままでもホーム画面追加 (PWA 化) は可能

## 設定ファイル

### `backend/config.json`

```json
{
  "agents": {
    "session_default": {
      "cwd": "/path/to/workdir",
      "model": "Opus",
      "launch_alias": "my_alias"
    }
  },
  "claude_path": "/path/to/claude",
  "rate_limits_log": "/path/to/rate-limits.jsonl",
  "notification_title": "Claude",
  "cors_allow_origins": []
}
```

- `claude_path`: `claude` コマンドの絶対パス (`which claude` で確認)。 PTY 起動時の存在
  検証に利用する。 未設定または不正パスの場合は起動を拒否する。 conda 等で PATH が
  通らない環境では明示する
- 各エージェントの `cwd` に配置された `CLAUDE.md` は `claude` 起動時に自動ロードされる
- `launch_alias` (任意): タブを新規作成した際に tmux pane へ自動入力する文字列。
  `~/.zshrc` 等に `alias my_alias='cd /path/to/workdir && claude'` のような起動ラッパを
  定義しておくと、 タブを開いた直後に claude TUI まで自動で立ち上がる。 未指定の場合は
  シェルプロンプトで停止し手動入力を待つ。 既存 tmux session への再接続時 (backend 再起動
  跨ぎ / タブ切替後) は claude が継続稼働している前提で何も送信しない
- `cors_allow_origins`: 通常は `[]` (backend が同一オリジンで frontend を配信するため
  CORS は不要)。 Vite dev server からアクセスする場合は `["http://localhost:5173"]` 等を
  設定する

### `frontend/.env` / `frontend/.env.local`

- **`frontend/.env`**: リポジトリにコミットされる既定値 (アプリ名 / アイコン等)
- **`frontend/.env.local`**: gitignore 済の個人用オーバーライド。 例:

```
VITE_API_BASE=https://<your-host>.tail<xxxx>.ts.net
```

`VITE_API_BASE` が未設定の場合は同一オリジンの相対 URL になる (backend が frontend を
配信する標準構成では設定不要)。 backend と frontend を別オリジンで運用する場合のみ
明示的に設定する。

## ディレクトリ構成

```
claude-pwa-client/
├── backend/                       # FastAPI バックエンド (Python)
│   ├── main.py                    # エントリポイント + ルータ集約 + lifespan task
│   ├── pty_runner.py              # claude を実 PTY + tmux で起動・駆動
│   ├── pty_discover.py            # tmux pane 配下の claude プロセス探索
│   ├── pty_routes.py              # /ws/pty (ターミナル) + /pty/{sid}/send (入力経路)
│   ├── control_mode.py            # tmux control mode (-CC) プロトコルパーサ
│   ├── jsonl_routes.py            # /jsonl/stream の SSE 配信 + 全 session tail loop
│   ├── jsonl_tail.py              # JSONL tail プリミティブ (純粋関数)
│   ├── jsonl_events.py            # JSONL 1 行 → chat UI イベント変換
│   ├── jsonl_session_status.py    # busy / agent_status / subagent の更新
│   ├── jsonl_notifications.py     # 停止要因の検出と Web Push 配信
│   ├── jsonl_plan_choices.py      # ExitPlanMode の選択肢抽出
│   ├── jsonl_watcher.py           # ~/.claude/projects 監視で session ↔ JSONL を紐付け
│   ├── chat_routes.py             # session メタ / status / 全 session overview SSE
│   ├── hooks_router.py            # /hooks/event (claude CLI hooks → Web Push)
│   ├── files_routes.py            # /file, /files/tree
│   ├── push.py                    # Web Push + 通知履歴 + SSE listener
│   ├── usage.py                   # 使用率 (5h / 7d / ctx) 組み立て
│   ├── chat_content.py            # 添付ファイル保存 (uploads/tmp)
│   ├── state.py                   # プロセス共有状態
│   ├── config.example.json
│   └── requirements.txt
└── frontend/                      # React + Vite
    ├── src/
    │   ├── App.jsx                # ルートコンポーネント (タブ / チャット / 画面共有等)
    │   ├── components/
    │   │   ├── MoonlightFrame.jsx # 画面共有 iframe
    │   │   ├── SessionDrawer.jsx  # セッション一覧ドロワー
    │   │   ├── StatusBar.jsx      # 使用率表示
    │   │   ├── MessageItem.jsx    # 単一メッセージのレンダリング
    │   │   └── ...
    │   ├── hooks/                 # チャット / SSE / 永続化 hook 群
    │   └── utils/
    └── public/
        ├── manifest.template.json # PWA manifest (env から値注入)
        └── sw.js                  # Service Worker (Web Push 受信)
```

## ライセンス

Apache License 2.0 (`LICENSE` および `NOTICE` を参照)。

Sunshine / moonlight-web-stream は GPL-3.0 ライセンスだが、 本リポジトリはこれらを
バンドル ・ リンクしていない。 別プロセスとして起動し HTTP / WebRTC 経由で連携するため、
本リポジトリ自体に GPL の copyleft は波及しない (FSF GPL FAQ「プロセス分離は通常
derivative work には当たらない」に依拠)。

派生物では `NOTICE` を保持し、 改変した主要ファイルにその旨を明記すること
(Apache-2.0 §4)。

## 謝辞

- [Claude Code](https://docs.claude.com/en/docs/claude-code) — Anthropic 公式 CLI
- [Sunshine](https://github.com/LizardByte/Sunshine) — 自己ホスト型ゲームストリームサーバ
- [moonlight-web-stream](https://github.com/MrCreativ3001/moonlight-web-stream) — Sunshine をブラウザ WebRTC で受ける bridge
