"""Web Push 配信 + 関連エンドポイント。

- VAPID 鍵 / サブスクリプションの永続化
- ターン完了時に呼ばれる broadcast_push()
- /push/state, /push/vapid-public-key, /push/subscribe, /push/unsubscribe
- 未読数 (= app badge 用) の保持 + /notifications/read-all + /notifications/sync
  (= 通知履歴は持たない、 未読カウンタだけ。 PWA を開いた / 該当 session を見た時に 0 リセット)
"""
import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

try:
    from pywebpush import WebPushException, webpush
    _HAS_WEBPUSH = True
except ImportError:
    _HAS_WEBPUSH = False

from config import AGENTS, NOTIFICATION_TITLE_DEFAULT, VAPID_SUB
from state import atomic_write_text, sessions_meta

logger = logging.getLogger(__name__)
router = APIRouter()

VAPID_PATH = Path(__file__).parent / "vapid.json"
SUBSCRIPTIONS_PATH = Path(__file__).parent / "subscriptions.json"

# 未読カウンタ: broadcast_push のたびに +1、 PWA を開いた時 (= /push/state visible) や
# /notifications/read-all で 0 リセット。 通知履歴本体は保持しない (= 2026-05-16 改修で
# 通知センター UI を撤去したため、 アプリバッジ同期に必要な int 1 個だけ残す)。
# broadcast_push は async、 mark_all_read / sync_unread_count は sync handler
# (= FastAPI thread pool 上で並行実行) なので、 +1 と read-write を atomic にするための lock。
_unread_count_lock = threading.Lock()
unread_count: int = 0

# client visible 状態: 該当 session を見てる時の通知抑制判定用
client_states: dict[str, dict] = {}


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


async def broadcast_push(
    message: str,
    title: str | None = None,
    session_id: str | None = None,
) -> None:
    """登録済みの全 Web Push サブスクリプションに通知を送る + 未読カウンタ +1。

    アクティブに該当セッションを見てる client がいる時は OS 通知も未読カウンタ加算も
    スキップする (= 既に画面で読まれてる前提)。

    session_id を渡すと payload に sid + URL を含める。 通知タップ時に SW が
    chat の該当セッションを開く。
    """
    global unread_count

    # 抑制判定: いずれかの client がこのセッションを active 表示中なら通知不要
    if is_session_actively_viewed(session_id):
        return

    body_clean = sanitize_notif_body(message)
    notif_title = title or NOTIFICATION_TITLE_DEFAULT

    # 未読カウンタを +1 して payload に載せる (= sw.js が setAppBadge に使う、 端末側で
    # 再 fetch せずに badge 更新できる)。 sync handler との race を避けるため lock 配下で atomic に。
    with _unread_count_lock:
        unread_count += 1
        snapshot_count = unread_count

    if not _HAS_WEBPUSH or not vapid_config or not subscriptions:
        return

    private_b64 = vapid_config.get("private_b64")
    if not private_b64:
        return

    payload_dict = {
        "title": notif_title,
        "body": body_clean,
        "unread_count": snapshot_count,
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
    return {"ok": True}


# --- 未読カウンタ API (= 通知履歴は持たない、 badge 同期用の数値だけ) ---
@router.post("/notifications/read-all")
def mark_all_read(payload: dict = Body(default={})):
    """未読カウンタを 0 にリセット。 PWA を開いた時 / session を開いた時に呼ばれる。
    payload の session_id は legacy 互換で受け取るが、 履歴を持たないので無視する。"""
    global unread_count
    with _unread_count_lock:
        before = unread_count
        unread_count = 0
    return {"ok": True, "count": before}


@router.post("/notifications/sync")
def sync_unread_count(payload: dict = Body(default={})):
    """未読カウンタを frontend から渡された現存数で上書きする。

    iOS PWA の通知センターに残ってる通知数 (= `registration.getNotifications()` の length)
    を frontend が visibility 復帰時に POST する。 push のたびに +1 してきた累積カウンタが
    PWA 起動時の見た目と乖離するのを防ぐ (= push 通知をユーザが一括消去した時の同期)。"""
    global unread_count
    try:
        count = int(payload.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    with _unread_count_lock:
        before = unread_count
        unread_count = count
        new_value = unread_count
    return {"ok": True, "before": before, "count": new_value}


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
