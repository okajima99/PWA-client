# Claude PWA Client

Claude Code をスマートフォンから操作するための PWA クライアント。 Mac で動かしてる
バックエンドに Tailscale 経由で iPhone / Android のブラウザから繋ぎ、 ホーム画面に
追加すれば普通のチャットアプリのように使える。

> ⚠️ **個人開発・自分用に作ってます**。 そのまま誰でも動くようには整えてません。
> 使ってみたい人向けに手順は書いてあるけど、 サポートは無し、 issue / PR は基本見ません。
> 動かなくても怒らないでね。

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

- **Mac デスクトップの画面共有**: PWA 内に [Sunshine](https://github.com/LizardByte/Sunshine)
  + [moonlight-web-stream](https://github.com/MrCreativ3001/moonlight-web-stream) 経由で
  Mac の画面を映す。 タッチ操作で Mac を遠隔操作できる。 セットアップ手順は後述

## アーキテクチャ

```
[スマートフォン]                  [Mac (= 開発機)]
                                ┌──────────────────────┐
   PWA (Safari/Chrome) ─────┐   │ FastAPI backend       │
       │                     │   │   ├ Claude Code CLI   │
       │                     ├─▶│   │   subprocess        │
       │                     │   │   └ Web Push (VAPID)   │
   ホーム画面追加で          │   │                       │
   standalone 起動           │   │ moonlight-web-stream  │ ← 任意
                              │   │   └ Sunshine          │ ← 任意
                              │   └──────────────────────┘
                              │              ↕ Tailscale
                              └──────────────┘
```

- バックエンドは Mac 上で常駐、 Claude Code CLI を subprocess として呼び出す
- iPhone / Android からは Tailscale 経由で Mac の HTTPS にアクセス
- インターネット公開はしない、 Tailscale tailnet 内のみ

## セットアップ

2 段階の構成。 **Path A** はチャット + 通知だけのシンプル版、 **Path B** はそれに加えて
Mac 画面共有まで使う上位版。

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

#### スマホから接続

1. Tailscale で Mac の MagicDNS 名を確認 (例: `your-host.tail<xxxx>.ts.net`)
2. スマホで `https://<your-host>.tail<xxxx>.ts.net/` を開く
3. iOS Safari なら 共有 → ホーム画面に追加 で PWA 化
4. 通知を有効にしたい場合は ⋯ メニューから「通知を有効にする」 (iOS は 16.4+ +
   ホーム画面追加が必須)

### Path B: Mac 画面共有も追加 (= 開発者向けオプション)

> ⚠️ **optional / 開発者向け**。 Rust nightly + 30 分の自前ビルドが要るので、
> 「自分の Mac の画面を自分のスマホで遠隔操作したい」 という明確な用途がある人だけ
> 進めてください。 chat + 通知だけ使う人は Path A で完結します。

Path A の構成に加えて、 Sunshine + moonlight-web-stream を Mac に install すると
PWA 内の 🖥 ボタンから Mac の画面共有 + タッチ遠隔操作が動く。

#### Sunshine (画面キャプチャ + Moonlight protocol サーバ)

```bash
# Homebrew で install
brew tap LizardByte/homebrew
brew install sunshine-beta

# 初回起動して config UI でユーザ作成 + アプリ登録 (= "Desktop" がデフォルトで入る)
sunshine
# ブラウザで https://localhost:47990 → 管理者アカウント作成
```

macOS の System Settings → プライバシーとセキュリティ → 画面録画 で sunshine に
許可を与える。 LaunchAgent で自動起動させる場合は `~/Library/LaunchAgents/` に
plist を置く (TCC 許可は手動が安全)。

#### moonlight-web-stream (= Sunshine ↔ ブラウザ WebRTC bridge)

macOS バイナリは公式提供されてないので Rust から自前ビルド。

```bash
# Rust nightly install
brew install rustup
rustup default nightly

# clone + build
git clone --recurse-submodules https://github.com/MrCreativ3001/moonlight-web-stream.git
cd moonlight-web-stream
cargo build --release   # 30 分くらい
npm install
npm run build
cp -r dist static  # release mode は static/ を見る

# 起動
./target/release/web-server
# ブラウザで http://localhost:8080/ → ユーザ作成 → host に localhost を追加 →
# Sunshine 側で PIN 入力でペアリング
```

#### Tailscale Serve で同一オリジン公開

PWA から `/moonlight/` 配下にプロキシで届くよう Tailscale Serve を設定:

```bash
# claude-pwa-client backend は :8000、 moonlight-web-stream は :8080
tailscale serve --bg http://localhost:8000
tailscale serve --bg --set-path=/moonlight http://localhost:8080/moonlight
```

moonlight-web-stream 側の `server/config.json` で `url_path_prefix` を `/moonlight` に
設定 + `default_user_id` を自動ログイン用に設定すると PWA の iframe が即起動する。

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
