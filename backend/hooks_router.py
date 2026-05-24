"""claude CLI hooks の受信エンドポイント (= phase 5 Web Push 接続経路)。

claude CLI は `~/.claude/settings.json` の `hooks` 配下に登録された command を、
イベント発生時に shell で起動し、 JSON payload を stdin で渡す
(= docs/pty-migration.md §10.2、 https://code.claude.com/docs/en/hooks)。

設定側 (= ユーザ環境) で `Stop` / `Notification` 等の hook に
    curl -sS -X POST http://localhost:8000/hooks/event --data-binary @-
を仕込めば、 本エンドポイントが受け取って Web Push に翻訳する。 これにより
PTY 経路 (= penalty 回避) で動かしてる claude でも、 turn 完了 / 通知ダイアログ
等の契機を PWA 側に届けられる。

session_id 解決:
    claude payload の `session_id` は claude 内部の uuid であり PWA session
    (= chat タブ識別子) とは別。 PWA session への解決は payload の `cwd` →
    AGENTS[<pwa_id>].cwd の逆引きで行う。 マッチしなければ「default」 (= 最初の
    agent) にフォールバック。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request

from config import AGENTS
from push import broadcast_push, notification_title_for

logger = logging.getLogger(__name__)

router = APIRouter()


def _pwa_session_for_cwd(cwd: str | None) -> str | None:
    """claude hook payload の cwd を PWA session id に逆引き。

    一致条件: 正規化済 path 同士の前方一致 (= cwd が agent.cwd 配下なら拾う)。
    マッチが無ければ None を返す、 呼び出し側で fallback 判断する。
    """
    if not cwd:
        return None
    try:
        target = Path(cwd).resolve()
    except OSError:
        return None
    for pwa_id, agent_cfg in AGENTS.items():
        agent_cwd = agent_cfg.get("cwd")
        if not agent_cwd:
            continue
        try:
            agent_path = Path(agent_cwd).expanduser().resolve()
        except OSError:
            continue
        try:
            target.relative_to(agent_path)
        except ValueError:
            continue
        return pwa_id
    return None


def _truncate(text: str, limit: int = 140) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


@router.post("/hooks/event")
async def hooks_event(request: Request) -> dict:
    """claude CLI が hook で叩いてくる JSON を受けて Web Push に変換する。

    対応イベント (= 初期実装):
        - Stop: turn 完了。 payload.output を通知本文に
        - Notification: 通知ダイアログ系。 payload.message を通知本文に
    その他のイベントは受信ログだけ残して 200 OK を返す (= claude の hook を
    詰まらせない、 後で対応イベントを増やしていく)。
    """
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        logger.warning("hooks/event: invalid JSON body, len=%d", len(body))
        return {"ok": False, "reason": "invalid_json"}

    event = payload.get("hook_event_name") or "?"
    claude_sid = payload.get("session_id")
    cwd = payload.get("cwd")
    pwa_session_id = _pwa_session_for_cwd(cwd)
    if pwa_session_id is None and AGENTS:
        # フォールバック: 最初の agent (= 設定上 default 扱い)
        pwa_session_id = next(iter(AGENTS.keys()))

    logger.info(
        "hooks/event recv: event=%s claude_sid=%s cwd=%s -> pwa_sid=%s",
        event, claude_sid, cwd, pwa_session_id,
    )

    if pwa_session_id is None:
        # AGENTS が空 = 何もできない、 ただ ack する
        return {"ok": True, "ignored": "no_agent"}

    title = notification_title_for(pwa_session_id)

    if event == "Stop":
        # Stop hook payload は `last_assistant_message` に直近の assistant 発言全文を
        # 載せる (= 2026-05-24 実機ダンプで確認、 spec で `output` ではない)。
        body_raw = payload.get("last_assistant_message") or payload.get("output") or ""
        body = _truncate(body_raw) if body_raw else "(turn 完了)"
        await broadcast_push(body, title, pwa_session_id)
        return {"ok": True, "pushed": "Stop"}

    if event == "Notification":
        message = payload.get("message") or ""
        if not message:
            return {"ok": True, "ignored": "empty_message"}
        body = _truncate(message)
        await broadcast_push(body, title, pwa_session_id)
        return {"ok": True, "pushed": "Notification"}

    # 未対応イベントは受信ログだけ残して通す
    return {"ok": True, "ignored": event}
