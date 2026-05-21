"""アプリ設定の読み込みと、複数モジュールから参照する定数。"""
import json
from pathlib import Path

HOME = Path.home()

CONFIG_PATH = Path(__file__).parent / "config.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

# --- agent 定義 ---
AGENTS: dict = config["agents"]

# --- ファイル系 ---
# uploads_tmp は config.json で上書き可能。
UPLOADS_TMP = Path(config.get("uploads_tmp", str(HOME / ".claude-pwa-client" / "uploads" / "tmp"))).expanduser()
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
FILE_SIZE_LIMIT = 1 * 1024 * 1024  # 1MB

# --- Anthropic 直結 ---
ANTHROPIC_API_BASE = "https://api.anthropic.com"
CLAUDE_PATH = config.get("claude_path")

# --- CORS ---
# CORS で許可するオリジン。 未設定 ( = config.json に cors_allow_origins キー無し) なら
# 空リストで CORS middleware を有効化しない、 つまり同一オリジン (= backend 配信の frontend)
# からのアクセスのみ通る (= 本番デフォルト)。 Vite dev server から叩く時は
# config.json で `["http://localhost:5173"]` 等を明示して dev 環境だけ開く。
CORS_ALLOW_ORIGINS: list = config.get("cors_allow_origins", [])

# --- 観測 ---
# Anthropic API レスポンス毎 (= ResultMessage 受信時) に shared rate-limit と usage を
# JSONL で永続化するファイル。 PWA 経由の token 消費 / 5h / 7d 使用率を時系列で観察する
# 用途。 path が空 / 未設定なら no-op (= backend は何も書かない)。
RATE_LIMITS_LOG_PATH = config.get("rate_limits_log", "")

# --- Web Push 関連 ---
# VAPID claim の sub (連絡先)。デフォルトは汎用 mailto。
VAPID_SUB = config.get("vapid_sub", "mailto:admin@example.com")
# OS 通知のタイトル既定値。エージェント別は agents.<name>.notification_title で上書き。
NOTIFICATION_TITLE_DEFAULT = config.get("notification_title", "Notification")
