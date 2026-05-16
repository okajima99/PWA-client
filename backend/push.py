"""Web Push 配信 + 関連エンドポイント。

- VAPID 鍵 / サブスクリプションの永続化
- ターン完了時に呼ばれる broadcast_push()
- /push/state, /push/vapid-public-key, /push/subscribe, /push/unsubscribe
- 通知履歴 (notifications.json) + /notifications API
"""
import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

try:
    from pywebpush import WebPushException, webpush
    _HAS_WEBPUSH = True
except ImportError:
    _HAS_WEBPUSH = False

from config import AGENTS, NOTIFICATION_TITLE_DEFAULT, VAPID_SUB
from state import atomic_write_text, flags, sessions_meta

logger = logging.getLogger(__name__)
router = APIRouter()

VAPID_PATH = Path(__file__).parent / "vapid.json"
SUBSCRIPTIONS_PATH = Path(__file__).parent / "subscriptions.json"
NOTIFICATIONS_PATH = Path(__file__).parent / "notifications.json"

# 通知履歴: 全 push 送信を蓄積、 PWA 通知センターから取得 / 既読化される
NOTIFICATIONS_MAX = 500  # 古いものから FIFO で切る上限
notifications_history: list[dict] = []
# SSE listener queue: 通知追加 / 既読化を購読中のクライアントに push する
_sse_listeners: list[asyncio.Queue] = []

# client visible 状態: 該当 session を見てる時の通知抑制判定用
client_states: dict[str, dict] = {}


def _load_notifications() -> list[dict]:
    if not NOTIFICATIONS_PATH.exists():
        return []
    try:
        data = json.loads(NOTIFICATIONS_PATH.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


_save_notifications_task: asyncio.Task | None = None
_NOTIF_FLUSH_DELAY = 5.0  # 秒、 高頻度時の I/O 圧縮


def _save_notifications_now() -> None:
    """同期的に notifications.json を atomic write する内部実装。"""
    if len(notifications_history) > NOTIFICATIONS_MAX:
        del notifications_history[: len(notifications_history) - NOTIFICATIONS_MAX]
    atomic_write_text(NOTIFICATIONS_PATH, json.dumps(notifications_history, ensure_ascii=False, indent=2))


async def _save_notifications_debounced() -> None:
    """N 秒後に flush。 既存の予約があれば cancel して再 schedule。"""
    try:
        await asyncio.sleep(_NOTIF_FLUSH_DELAY)
        _save_notifications_now()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("notifications debounce flush failed")


def _save_notifications() -> None:
    """notification 追加 / 既読化のたびに呼ばれる。 5 秒 debounce で I/O 圧縮。
    最終確実性のため lifespan 終了時にも flush 想定 (= 終了 hook で _save_notifications_now)。"""
    global _save_notifications_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # ループ外 (= startup 前等) なら同期 flush で fallback
        _save_notifications_now()
        return
    if _save_notifications_task and not _save_notifications_task.done():
        _save_notifications_task.cancel()
    _save_notifications_task = loop.create_task(_save_notifications_debounced())


def is_session_actively_viewed(session_id: str | None) -> bool:
    """指定 session を visible で見てる client がいるか。
    session_id が None なら 1 client でも visible なら True (legacy 互換)。
    """
    if not session_id:
        return any(s.get("visible") for s in client_states.values())
    for s in client_states.values():
        if s.get("visible") and s.get("session_id") == session_id:
            return True
    return False


def _broadcast_sse_event(event: dict) -> None:
    """通知履歴の変化を全 SSE listener に push する (queue にいれるだけ、 失敗は無視)"""
    for q in list(_sse_listeners):
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _load_vapid() -> dict | None:
    if not VAPID_PATH.exists():
        return None
    try:
        data = json.loads(VAPID_PATH.read_text())
    except Exception:
        logger.exception("Failed to parse vapid.json")
        return None
    # pywebpush.webpush() は内部で Vapid.from_string を呼ぶが、それは PEM
    # ヘッダ/フッタを剥がした base64 部分のみ受け付ける。起動時に 1 回だけ
    # 抽出しておき、配信ごとの再計算を避ける。
    pem = data.get("private_pem", "")
    if pem:
        data["private_b64"] = "".join(
            line for line in pem.splitlines() if not line.startswith("-----")
        ).strip()
    return data


def _load_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_subscriptions() -> None:
    atomic_write_text(SUBSCRIPTIONS_PATH, json.dumps(subscriptions, indent=2))


vapid_config: dict | None = _load_vapid()
subscriptions: list[dict] = _load_subscriptions()
notifications_history = _load_notifications()

_NOTIF_BODY_RE = re.compile(r"\s+")

# Markdown 記号 strip 用 (Web Push 通知はリッチテキストを描画できないので
# `#` `**bold**` などの記号がそのまま見えてしまう。読みやすさを優先して記号を消す)
_MD_FENCE_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
# 表セパレータ行 (`|---|---|` `| :--- | ---: |` 等) は意味を持たないので削除
_MD_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$",
    re.MULTILINE,
)
# 表行 `| a | b | c |` をセル分かち書き `a / b / c` に変換
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$", re.MULTILINE)
_MD_PATTERNS = [
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),       # 見出し記号
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),               # bold
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),    # italic
    (re.compile(r"`([^`\n]+)`"), r"\1"),                   # inline code
    (re.compile(r"!?\[([^\]]+)\]\([^)]+\)"), r"\1"),       # [text](url) / ![alt](url)
    (re.compile(r"^[-*+]\s+", re.MULTILINE), "• "),        # 箇条書き → 中黒
    (re.compile(r"^\d+\.\s+", re.MULTILINE), ""),          # 番号付きリスト
    (re.compile(r"^>\s*", re.MULTILINE), ""),              # 引用
    (re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE), ""),     # 水平線
]


def _table_row_to_inline(m: re.Match) -> str:
    inner = m.group(1)
    cells = [c.strip() for c in inner.split("|")]
    cells = [c for c in cells if c]
    return " / ".join(cells)


def strip_markdown(text: str) -> str:
    """Markdown 記号を取り除いて素のテキストに近づける (loss-y、通知 body 用)。"""
    if not text:
        return text
    text = _MD_FENCE_RE.sub(lambda m: m.group(1), text)
    # 表対応はパターン適用前に: セパレータ行を消し、 残った行をセル分かち書きへ
    text = _MD_TABLE_SEP_RE.sub("", text)
    text = _MD_TABLE_ROW_RE.sub(_table_row_to_inline, text)
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def sanitize_notif_body(text: str) -> str:
    """通知 body 用の整形。Markdown 記号を消し、改行・連続空白を 1 スペースに畳む。
    iOS のロック画面通知は 1 行表示で、生改行や Markdown 記号が入ると見え方が崩れる。
    """
    if not text:
        return ""
    text = strip_markdown(text)
    return _NOTIF_BODY_RE.sub(" ", text).strip()


_NOTIF_TITLE_MAX = 32


def _trim_title(title: str) -> str:
    """iOS のロック画面通知タイトルは ~30 文字程度で切れるので 32 文字でカット。"""
    if not title:
        return title
    if len(title) <= _NOTIF_TITLE_MAX:
        return title
    return title[: _NOTIF_TITLE_MAX - 1] + "…"


def notification_title_for(session_id: str) -> str:
    """通知タイトル: セッション title を最優先、 fallback で agent の notification_title。
    iOS のロック画面で見切れない長さに trim する。"""
    meta = sessions_meta.get(session_id)
    if meta:
        if meta.title:
            return _trim_title(meta.title)
        cfg = AGENTS.get(meta.agent_id) or {}
        return cfg.get("notification_title") or NOTIFICATION_TITLE_DEFAULT
    return NOTIFICATION_TITLE_DEFAULT


async def broadcast_push(
    message: str,
    title: str | None = None,
    session_id: str | None = None,
) -> None:
    """登録済みの全 Web Push サブスクリプションに通知を送る + 通知履歴に記録。

    アクティブに該当セッションを見てる client がいる時は OS 通知も履歴も
    スキップする (= 既に画面で読まれてる前提、 重複させない)。

    session_id を渡すと payload に sid + URL を含める。 通知タップ時に SW が
    chat の該当セッションを開く。
    """
    # 抑制判定: いずれかの client がこのセッションを active 表示中なら通知不要
    if is_session_actively_viewed(session_id):
        return

    body_clean = sanitize_notif_body(message)
    notif_title = title or NOTIFICATION_TITLE_DEFAULT

    # 通知履歴に追記 (Web Push が無効でも履歴は残す = 通知センターで見える)
    notif_id = uuid.uuid4().hex[:12]
    notif_record = {
        "id": notif_id,
        "ts": time.time(),
        "session_id": session_id,
        "title": notif_title,
        "body": body_clean,
        "read": False,
    }
    notifications_history.append(notif_record)
    try:
        _save_notifications()
    except Exception:
        logger.exception("notification save failed")
    _broadcast_sse_event({"type": "added", "notification": notif_record})

    if not _HAS_WEBPUSH or not vapid_config or not subscriptions:
        return

    private_b64 = vapid_config.get("private_b64")
    if not private_b64:
        return

    # 未読数を payload に載せて SW でバッジ更新できるようにする (= fetch 不要、 省電力)
    unread_count = sum(1 for n in notifications_history if not n.get("read"))

    payload_dict = {
        "id": notif_id,
        "title": notif_title,
        "body": body_clean,
        "unread_count": unread_count,
    }
    if session_id:
        payload_dict["sid"] = session_id
        payload_dict["url"] = f"/?ses={session_id}"
    payload = json.dumps(payload_dict, ensure_ascii=False)
    dead: list[dict] = []

    def _send_one(sub: dict) -> None:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_b64,
                vapid_claims={"sub": VAPID_SUB},
                ttl=60,
            )
        except WebPushException as e:
            # 410 Gone / 404 → サブスクリプションが端末で破棄された、削除候補
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status in (404, 410):
                dead.append(sub)
            else:
                logger.warning("webpush failed (status=%s): %s", status, e)
        except Exception:
            logger.exception("webpush send error")

    # pywebpush は同期 API なので thread pool に逃がす
    await asyncio.gather(*(asyncio.to_thread(_send_one, s) for s in list(subscriptions)))

    if dead:
        for d in dead:
            try:
                subscriptions.remove(d)
            except ValueError:
                pass
        _save_subscriptions()


# --- エンドポイント ---
@router.post("/push/state")
def push_state(payload: dict = Body(...)):
    """visibilitychange / activeSession 変化イベントで呼ばれる。

    request body:
      - visible: bool   フォアグラウンド (= 通知不要) かどうか
      - session_id: str  現在見てるセッション id (可視時のみ意味あり)
      - client: 後方互換のため受け取るが内部では使わない

    broadcast_push 時に「該当 session を見てる client がいるなら抑制」 判定に使う。
    """
    visible = bool(payload.get("visible"))
    session_id = payload.get("session_id")
    client = payload.get("client") or "web"
    client_states[client] = {
        "visible": visible,
        "session_id": session_id if visible else None,
        "ts": time.time(),
    }
    # legacy 互換: いずれかの client が visible なら user_visible=True
    flags["user_visible"] = any(s.get("visible") for s in client_states.values())
    return {"ok": True}


# --- 通知センター API ---
@router.get("/notifications")
def list_notifications(limit: int = 50, unread_only: bool = False):
    """通知履歴を新しい順で返す。 PWA 通知センターから取得される。
    response に unread_count を含めるので app badge 同期に使える。"""
    items = list(reversed(notifications_history))
    if unread_only:
        items = [n for n in items if not n.get("read")]
    unread_count = sum(1 for n in notifications_history if not n.get("read"))
    return {
        "notifications": items[: max(1, min(limit, NOTIFICATIONS_MAX))],
        "unread_count": unread_count,
    }


@router.get("/notifications/unread-count")
def get_unread_count():
    """未読数だけ返す軽量エンドポイント。 app badge 同期 / 起動時 fetch 用。"""
    return {"unread_count": sum(1 for n in notifications_history if not n.get("read"))}


@router.post("/notifications/{notif_id}/read")
def mark_notification_read(notif_id: str):
    """単一通知を既読化。 PWA で通知タップ時に呼ばれる。"""
    changed = False
    for n in notifications_history:
        if n.get("id") == notif_id and not n.get("read"):
            n["read"] = True
            changed = True
            break
    if changed:
        try:
            _save_notifications()
        except Exception:
            logger.exception("notification save failed")
        _broadcast_sse_event({"type": "read", "ids": [notif_id]})
    return {"ok": True, "changed": changed}


@router.post("/notifications/read-all")
def mark_all_read(payload: dict = Body(default={})):
    """まとめて既読化。 session_id 指定時はそのセッション分だけ。"""
    target_sid = payload.get("session_id")
    changed_ids: list[str] = []
    for n in notifications_history:
        if n.get("read"):
            continue
        if target_sid is not None and n.get("session_id") != target_sid:
            continue
        n["read"] = True
        changed_ids.append(n.get("id"))
    if changed_ids:
        try:
            _save_notifications()
        except Exception:
            logger.exception("notification save failed")
        _broadcast_sse_event({"type": "read", "ids": changed_ids})
    return {"ok": True, "count": len(changed_ids)}


@router.delete("/notifications/{notif_id}")
def delete_notification(notif_id: str):
    """通知を削除 (履歴から消す)。"""
    before = len(notifications_history)
    notifications_history[:] = [n for n in notifications_history if n.get("id") != notif_id]
    if len(notifications_history) != before:
        try:
            _save_notifications()
        except Exception:
            logger.exception("notification save failed")
        _broadcast_sse_event({"type": "removed", "ids": [notif_id]})
    return {"ok": True}


@router.get("/notifications/stream")
async def notifications_stream():
    """SSE: 通知の追加 / 既読 / 削除を全 PWA タブにリアルタイム配信。"""
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue = asyncio.Queue()
    _sse_listeners.append(queue)

    async def gen():
        try:
            # 接続直後にハンドシェイク (= retry 設定 + 接続確認)
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # keep-alive ping (一部 proxy が idle connection を切るので 25 秒毎に)
                    yield ": ping\n\n"
        finally:
            try:
                _sse_listeners.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/push/vapid-public-key")
def get_vapid_public_key():
    if not vapid_config or not vapid_config.get("public_key"):
        raise HTTPException(status_code=503, detail="VAPID not configured. Run gen_vapid.py.")
    return {"public_key": vapid_config["public_key"]}


def _sub_key(sub: dict) -> str | None:
    """サブスクリプションのユニーク識別子 (endpoint URL)。"""
    if not isinstance(sub, dict):
        return None
    return sub.get("endpoint")


@router.post("/push/subscribe")
def push_subscribe(subscription: dict = Body(...)):
    key = _sub_key(subscription)
    if not key:
        raise HTTPException(status_code=400, detail="Invalid subscription (missing endpoint)")
    # endpoint で重複排除
    for i, s in enumerate(subscriptions):
        if _sub_key(s) == key:
            subscriptions[i] = subscription
            break
    else:
        subscriptions.append(subscription)
    _save_subscriptions()
    return {"ok": True, "count": len(subscriptions)}


@router.post("/push/unsubscribe")
def push_unsubscribe(subscription: dict = Body(...)):
    key = _sub_key(subscription)
    if not key:
        raise HTTPException(status_code=400, detail="Invalid subscription (missing endpoint)")
    before = len(subscriptions)
    subscriptions[:] = [s for s in subscriptions if _sub_key(s) != key]
    if len(subscriptions) != before:
        _save_subscriptions()
    return {"ok": True, "count": len(subscriptions)}
