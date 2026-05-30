"""FastAPI app のエントリポイント。
ロギング初期化 → ルータ登録 → 静的ファイル配信、を組み立てるだけ。
ビジネスロジックは下記の責務別モジュールに分かれている:

- config.py        設定 / 定数
- state.py         プロセス共有状態
- pty_runner.py    PTY-attached claude CLI 駆動 (= tmux 内で claude TUI を起動)
- chat_routes.py   session メタ / status / config エンドポイント
- pty_routes.py    /ws/pty/{session_id} WebSocket + /pty/{sid}/send (= 入力経路)
- jsonl_routes.py  /jsonl/stream/{sid} SSE (= JSONL tail で出力配信) + blocker 監視
- hooks_router.py  /hooks/event (= claude CLI hooks → Web Push)
- files_routes.py  ファイル系エンドポイント
- push.py          Web Push 配信 + エンドポイント
"""
import asyncio
import logging
import logging.handlers
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# --- ロギング初期化 (各モジュール import より前) ---
# 全 logger は RotatingFileHandler で自動 rotate (= ファイルサイズ上限を 5MB、 過去 3 世代まで
# 保持)。 backend を長時間稼働させても backend.error.log は最大 ~20MB で頭打ち。
# 加えて uvicorn の access log (= 通常 stdout に流れて LaunchAgent の StandardOutPath で
# backend.log に永続 append されてた、 過去 18MB まで膨らんだ実績) も同じ機構に流す
# (= setup_uvicorn_access_log() で別 file に rotation 付きで投入)。
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG_PATH = LOG_DIR / "backend.error.log"
ACCESS_LOG_PATH = LOG_DIR / "backend.access.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5MB / file
LOG_BACKUP_COUNT = 3              # 過去 3 世代 (= 合計 ~20MB 上限)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _make_rotating_handler(path: Path) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    h.setFormatter(logging.Formatter(_LOG_FORMAT))
    return h


_root = logging.getLogger()
_root.setLevel(logging.INFO)
# basicConfig だと既設 handler があれば no-op になるので addHandler で明示
_root.addHandler(_make_rotating_handler(ERROR_LOG_PATH))


def setup_uvicorn_access_log() -> None:
    """uvicorn の access logger を専用 rotating file に向ける。
    LaunchAgent の StandardOutPath (= backend.log) に永続 append されてた経路を断つ。"""
    access = logging.getLogger("uvicorn.access")
    # propagate=False で root logger (= error.log) への二重出力を防ぐ
    access.propagate = False
    access.setLevel(logging.INFO)
    # 既存 handler が居れば外す (= uvicorn が StreamHandler を default で付ける)
    for h in list(access.handlers):
        access.removeHandler(h)
    access.addHandler(_make_rotating_handler(ACCESS_LOG_PATH))


setup_uvicorn_access_log()
logger = logging.getLogger(__name__)

# --- アプリ内モジュール ---
from config import CORS_ALLOW_ORIGINS, UPLOADS_TMP  # noqa: E402
from state import sessions_meta  # noqa: E402

import chat_routes  # noqa: E402
import files_routes  # noqa: E402
import hooks_router  # noqa: E402
import jsonl_routes  # noqa: E402
import pty_routes  # noqa: E402
import pty_runner  # noqa: E402
import push  # noqa: E402


def _truncate_if_oversized(path: Path, max_bytes: int) -> None:
    """launchd の StandardOutPath は app の RotatingFileHandler と別管理で自動 rotate
    されない。 起動時に上限超過してたら 0 に切り詰める (= 同 inode を保つので launchd の
    O_APPEND fd は次 write から先頭に append し直し、 単調増加を防ぐ)。"""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            open(path, "w").close()
    except OSError:
        logger.debug("truncate oversized log failed: %s", path, exc_info=True)


def _prune_uploads_tmp(max_age_sec: float = 24 * 3600) -> None:
    """uploads/tmp の古い添付ファイルを掃除する。 セッション削除時にも消えるが、
    削除しない運用 + 無停止 backend だと溜まるので定期 + 起動時に sweep する。"""
    if not UPLOADS_TMP.exists():
        return
    cutoff = time.time() - max_age_sec
    for f in UPLOADS_TMP.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            logger.debug("upload tmp unlink failed: %s", f, exc_info=True)


async def _uploads_tmp_gc_loop(interval_sec: float = 3600.0) -> None:
    """1 時間ごとに uploads/tmp を sweep する軽量 GC タスク。"""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            _prune_uploads_tmp()
        except Exception:
            logger.exception("uploads tmp gc failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動: 古い tmp ファイル / 大きすぎるエラーログの掃除 + 各種 background task 起動

    # tmux server の status bar を全 session で OFF にする (= 端末画面下の緑バー除去)。
    # `-g` でサーバ全体に効くので、 既存 session も新規 session も等しく status off。
    # tmux 未起動 / 未インストール時は黙って失敗させる。
    if pty_runner.USE_TMUX_WRAP:
        try:
            import subprocess as _sp
            _sp.run(
                [pty_runner.TMUX_BIN, "set-option", "-g", "status", "off"],
                check=False, timeout=2,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
        except Exception:
            logger.debug("tmux status off failed", exc_info=True)

    import asyncio as _asyncio

    # 常時 tail: PWA 接続有無に関係なく全 sid の JSONL を監視して、 AskUserQuestion /
    # stop_reason 異常を Web Push に流す (= jsonl_routes SSE 経路と独立)。
    blocker_monitor_task = _asyncio.create_task(jsonl_routes.monitor_all_sessions_loop())

    # 番号待ち TUI プロンプト (= モデル切替確認 / survey / 許可 等、 Stop でも
    # AskUserQuestion でもないやつ) を capture-pane で検出して pending_prompt に載せ、
    # status SSE + Web Push でチャットに出す (= ターミナルに切り替えなくても気づける)。
    import prompt_watch  # noqa: PLC0415, E402
    prompt_watch_task = _asyncio.create_task(prompt_watch.prompt_watch_loop())

    # JSONL watcher: ~/.claude/projects/ を fsevents で監視して、 各 PWA session の
    # claude プロセスが書く JSONL を backend mem に確定保持する。
    import jsonl_watcher  # noqa: PLC0415, E402
    jsonl_watcher.start_watcher()
    # 既存 tmux session (= backend 再起動跨ぎ) の claude プロセスを registry に登録
    for sid in list(sessions_meta.keys()):
        _asyncio.create_task(pty_runner._register_claude_when_ready(sid))

    # uploads/tmp: 起動時 sweep + 1 時間ごとの定期 GC (= 無停止運用でも溜め続けない)
    _prune_uploads_tmp()
    uploads_gc_task = _asyncio.create_task(_uploads_tmp_gc_loop())

    # サスティナビリティ整備: stale tmux session kill / 古い JSONL 削除 / statusline map
    # cleanup を起動時に 1 回 + 24 時間ごとに実行 (= 放置すると無限蓄積する分の自動整理)。
    import maintenance  # noqa: PLC0415, E402
    try:
        startup_summary = maintenance.run_all_maintenance()
        logger.info("startup maintenance: %s", startup_summary)
    except Exception:
        logger.exception("startup maintenance failed")
    maintenance_task = _asyncio.create_task(maintenance.maintenance_loop())

    # backend.error.log / backend.access.log は RotatingFileHandler で自動 rotate。
    # launchd の StandardOutPath (= backend.log) / StandardErrorPath (= backend.boot.log) は
    # launchd 管理で rotate されないので起動時に上限超過を切る (= app の rotate 系とは別ファイル)。
    for _log_name in ("backend.log", "backend.boot.log"):
        _truncate_if_oversized(LOG_DIR / _log_name, LOG_MAX_BYTES)

    yield

    # 終了: 常時 tail task + uploads GC task + maintenance task を停止 → PTY セッションを閉じる
    blocker_monitor_task.cancel()
    prompt_watch_task.cancel()
    uploads_gc_task.cancel()
    maintenance_task.cancel()
    for _t in (blocker_monitor_task, prompt_watch_task, uploads_gc_task, maintenance_task):
        try:
            await _t
        except (asyncio.CancelledError, Exception):
            # cancel 後の CancelledError は想定通り、 それ以外の例外は無視 (= shutdown 続行)。
            pass
    await pty_runner.shutdown_all()
    import jsonl_watcher  # noqa: PLC0415, E402
    jsonl_watcher.stop_watcher()


app = FastAPI(lifespan=lifespan)

# frontend は backend で配信される設計なので、 通常運用では同一オリジン = CORS 不要。
# config に明示指定があった時だけ middleware を有効化 (= dev で vite から叩く等)。
if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(chat_routes.router)
app.include_router(files_routes.router)
app.include_router(hooks_router.router)
app.include_router(jsonl_routes.router)
app.include_router(pty_routes.router)
app.include_router(push.router)


# --- 静的ファイル配信 (Vite ビルド成果物) ---
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


class CacheControlledStaticFiles(StaticFiles):
    """index.html / manifest.json / sw.js は no-cache、ハッシュ付き assets は immutable で長期キャッシュ。

    iOS Safari (PWA) はデフォルトで Cache-Control 無しレスポンスを長時間キャッシュするため、
    index.html が古いままになり Vite の新しいハッシュ付き assets ファイルを参照できなくなる。
    エントリポイント (= index.html / manifest.json / sw.js) だけ毎回鮮度確認させ、
    /assets/ 配下はファイル名にハッシュが入っているので永久キャッシュして問題ない。
    """

    NO_CACHE_PATHS = {"index.html", "manifest.json", "sw.js"}
    IMMUTABLE_PREFIX = "assets/"

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        normalized = path.lstrip("/")
        if normalized in self.NO_CACHE_PATHS or normalized in ("", "."):
            response.headers["Cache-Control"] = "no-cache"
        elif normalized.startswith(self.IMMUTABLE_PREFIX):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if FRONTEND_DIST.exists():
    app.mount(
        "/",
        CacheControlledStaticFiles(directory=str(FRONTEND_DIST), html=True),
        name="frontend",
    )
else:
    # frontend をビルドしてない状態で backend を立てると静的配信が無効、 ブラウザから
    # PWA を開けない (= API だけ生きる)。 起動ログに残して原因特定を早める。
    logging.getLogger(__name__).warning(
        "frontend dist not found at %s; PWA assets will not be served. "
        "Run `cd frontend && npm run build`.",
        FRONTEND_DIST,
    )
