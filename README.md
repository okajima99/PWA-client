# ClaudePWA

スマートフォンからローカルの AI エージェントとチャットするための PWA クライアントです。

## 概要

ターミナルクライアント（SSH + tmux）で AI エージェントを操作していたが、スマートフォンでのスクロール・フリック入力との相性が悪かった。ChatGPT アプリに近いチャット UI を PWA として自作し、ホーム画面から直接開けるアプリとして使えるようにした。

Claude Code CLI（`claude --resume`）をバックエンドの subprocess で呼び出し、2 エージェントのセッションを独立して並走させる。Tailscale 経由でスマートフォンからプライベートアクセスする構成のため、インターネットには公開しない。

## 機能

- **2 エージェントのタブ切り替え**: Agent A / Agent B のセッションが独立して並走する。タブを切り替えても、裏側のセッションは継続される
- **SSE ストリーミング**: Claude のレスポンスをリアルタイムでストリーミング表示
- **バックグラウンド処理**: スマートフォンの画面を閉じてもサーバー側で Claude の処理を継続。復帰時に自動再接続してバッファを受信する
- **ステータスバー**: コンテキスト使用率・5 時間 usage・7 日 usage・使用モデルをリアルタイム表示
- **ファイルプレビュー**: チャット内のファイルパスをタップしてモーダルで表示（Markdown レンダリング・シンタックスハイライト対応）
- **ファイルツリー**: サーバー上のディレクトリをパネルで閲覧
- **画像・テキストファイル添付**: マルチパートフォームで送信し、Claude に参照させる。送信済み画像は base64 で保存されリロード後も表示される
- **セッション永続化**: サーバー再起動後も session_id を引き継いで会話継続
- **メッセージ履歴の永続化**: lz-string 圧縮で localStorage に保存。最新 200 件を自動保持
- **launchd 自動起動**: macOS の launchd で uvicorn を常時起動管理

## アーキテクチャ

```
[スマートフォン（PWA）]
       ↕ Tailscale
[バックエンド: FastAPI（uvicorn）]
       ↕ subprocess
[Claude Code CLI: claude --resume]
       ↕ ANTHROPIC_BASE_URL
[Anthropic リバースプロキシ（FastAPI 内蔵）]
```

### バックエンド（Python / FastAPI）

- `POST /chat/{agent}/stream` — メッセージ送信 + SSE ストリーミングレスポンス
- `GET /chat/{agent}/reconnect` — バックグラウンド復帰時の再接続
- `POST /chat/{agent}/stop` — 応答の中断
- `POST /session/{agent}/end` — セッションリセット
- `GET /status/{agent}` — ステータスバー用 usage 情報
- `GET /file` / `GET /files/tree` — ファイル参照・ツリー表示
- `ANY /proxy/...` — Anthropic API リバースプロキシ（usage ヘッダー取得用）

Claude Code CLI を `--resume <session_id>` で呼び出し、セッション継続を実現。stdin に stream-json を流してレスポンスを SSE として中継する。

### フロントエンド（Vite + React）

- react-markdown + remark-gfm でチャット内の Markdown をレンダリング
- ファイルパスの自動リンク化（独自 remark プラグイン）
- react-syntax-highlighter（Prism）でファイルプレビューのシンタックスハイライト
- lz-string 圧縮 + localStorage でメッセージ履歴・バッファ位置を永続化（最新 200 件）
- rAF バッチングによりストリーミング中の再レンダリングを 1 フレーム 1 回に抑制
- Error Boundary でレンダリングエラーを捕捉し、blank screen を防止
- PWA: manifest.json + SVG アイコン。ホーム画面に追加してアプリとして利用
- プロダクションビルドをバックエンド（FastAPI）から配信（ポート 8000 のみ）

## ディレクトリ構成

```
pwa-client/
├── backend/
│   ├── main.py                  # FastAPI アプリケーション
│   ├── requirements.txt
│   ├── config.example.json      # 設定ファイルのサンプル（公開可）
│   ├── config.json              # ローカル設定（gitignore）
│   └── sessions.json            # session_id 永続化ファイル（gitignore）
├── frontend/
│   ├── src/
│   │   ├── App.jsx              # メインコンポーネント（タブ・チャット・ステータスバー）
│   │   ├── ErrorBoundary.jsx    # レンダリングエラー捕捉・リロードUI表示
│   │   ├── MessageRenderer.jsx  # Markdown レンダリング + ファイルパスリンク化
│   │   ├── FilePreviewModal.jsx # ファイルプレビューモーダル（シンタックスハイライト対応）
│   │   └── FileTreePanel.jsx    # ファイルツリーパネル
│   ├── public/
│   │   └── manifest.json        # PWA マニフェスト
│   └── index.html
└── docs/
    └── requirements.md          # 要件定義書
```

## セットアップ

このリポジトリはポートフォリオ目的で公開されており、そのままクローンして動かすことは想定していません。動作には以下のローカル環境が必要です。

### 必要なもの

- Python 3 + conda 環境（または venv）
- Claude Code CLI（`claude` コマンド）のセットアップと認証
- Node.js（フロントエンドのビルド用）
- Tailscale（スマートフォンアクセス用）

### バックエンド

```bash
# 依存パッケージのインストール
conda create -n pwa-client python=3.11
conda activate pwa-client
pip install -r backend/requirements.txt

# 設定ファイルの作成
cp backend/config.example.json backend/config.json
# config.json を編集（エージェントの cwd・claude コマンドパス等を設定）

# 起動
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### フロントエンド

```bash
cd frontend
npm install

# .env.local を作成してバックエンドの URL を設定
echo "VITE_API_BASE=http://<tailscale-ip>:8000" > .env.local

npm run build   # 本番ビルド（dist/ を生成。バックエンドが配信する）
npm run dev     # 開発サーバー（開発時のみ。本番は不要）
```

### launchd 自動起動（macOS）

`~/Library/LaunchAgents/` に LaunchAgent の plist を配置し、`launchctl load` で読み込みます。KeepAlive を有効にしてサーバーが落ちたら自動再起動させます。バックエンド（ポート 8000）がフロントエンドの静的ファイルも兼ねて配信します。

## 設定ファイル（config.json）

```json
{
  "agents": {
    "agent_a": {
      "cwd": "/path/to/agent_a/workdir",
      "model": "Opus"
    },
    "agent_b": {
      "cwd": "/path/to/agent_b/workdir",
      "model": "Sonnet"
    }
  },
  "rate_limits_log": "/path/to/rate-limits.jsonl"
}
```

各エージェントの `cwd` に置かれた `CLAUDE.md` が `claude` コマンド起動時に自動ロードされ、エージェントごとに異なる人格・コンテキストが注入される。

## ステータス

MVP 完成・運用中。スマートフォンのホーム画面から ClaudePWA アプリとして起動し、日常的に使用している。
