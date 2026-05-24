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

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request

from config import AGENTS, TMUX_SESSION_MAP_DIR
from push import broadcast_push, notification_title_for
from state import agent_status, stream_states

logger = logging.getLogger(__name__)

router = APIRouter()


_TMUX_MAP = Path(TMUX_SESSION_MAP_DIR).expanduser() if TMUX_SESSION_MAP_DIR else None


def _pwa_session_for_claude_sid(claude_sid: str | None) -> str | None:
    """claude が hook payload で渡してくる claude session_id (= JSONL ファイル名と一致)
    を逆引きして、 PWA のタブ識別子 (= `ses_xxxx`) を返す。 PWA 経由で起動した
    claude セッションだけが `tmux_session_map_dir` に登録される (= 起動時 statusline が
    書く)。 デスクトップ公式 / ターミナル直叩きは登録されないので、 ここで弾いて
    Web Push を抑制する。 マッチしなければ None。
    """
    if not claude_sid or _TMUX_MAP is None or not _TMUX_MAP.is_dir():
        return None
    for f in _TMUX_MAP.iterdir():
        if not f.is_file():
            continue
        try:
            if f.read_text(encoding="utf-8", errors="replace").strip() == claude_sid:
                name = f.name
                # ファイル名は `pwa-<session_id>` 規約 (= pty_runner._tmux_session_name)
                return name[4:] if name.startswith("pwa-") else name
        except OSError:
            continue
    return None


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

    # PWA 経由で起動した claude セッションだけ通知する。 claude CLI の hook 設定は
    # `~/.claude/settings.json` 経由でグローバルなので、 デスクトップ公式 / ターミナル
    # 直叩きでも curl が飛んでくる。 tmux_session_map に登録された claude_sid のみが
    # PWA タブ経由 (= statusline が起動時に書く) なので、 ここで厳密判別して除外する。
    pwa_session_id = _pwa_session_for_claude_sid(claude_sid)
    if pwa_session_id is None:
        # cwd フォールバックは不採用 (= デスクトップ公式が AGENTS と同じ cwd で動いた時に
        # 誤マッチして通知が飛ぶ要因だった)。 確実に PWA 経由でない claude は ack だけ。
        logger.info(
            "hooks/event ignored (not a PWA session): event=%s claude_sid=%s cwd=%s",
            event, claude_sid, cwd,
        )
        return {"ok": True, "ignored": "non_pwa_session"}

    logger.info(
        "hooks/event recv: event=%s claude_sid=%s cwd=%s -> pwa_sid=%s",
        event, claude_sid, cwd, pwa_session_id,
    )

    title = notification_title_for(pwa_session_id)

    if event == "Stop":
        # Stop hook payload は `last_assistant_message` に直近の assistant 発言全文を
        # 載せる (= 2026-05-24 実機ダンプで確認、 spec で `output` ではない)。
        body_raw = payload.get("last_assistant_message") or payload.get("output") or ""
        body = _truncate(body_raw) if body_raw else "(turn 完了)"
        # turn 完了の即時通知: agent_status の current_tool / subagent を解放 + status SSE を
        # 即発火することで、 JSONL の result 行 tail 待ち (= 数百 ms-数秒) を待たずに
        # PWA 側の停止ボタン → 送信ボタン切替を ms 単位で確定させる。
        a = agent_status.get(pwa_session_id)
        if a is not None:
            a["current_tool"] = None
            a["subagent"] = None
        state = stream_states.get(pwa_session_id)
        if state is not None:
            state.status_event.set()
        # fire-and-forget: webpush 送信は数百 ms かかる場合があり、 hook の curl が
        # それを待つと claude プロセスの Stop ハンドラ完了が遅れて体感も遅くなる。
        # backend は即 200 を返して、 配信は別 task に逃がす。
        asyncio.create_task(broadcast_push(body, title, pwa_session_id))
        return {"ok": True, "pushed": "Stop"}

    if event == "Notification":
        message = payload.get("message") or ""
        if not message:
            return {"ok": True, "ignored": "empty_message"}
        body = _truncate(message)
        asyncio.create_task(broadcast_push(body, title, pwa_session_id))
        return {"ok": True, "pushed": "Notification"}

    # 未対応イベントは受信ログだけ残して通す
    return {"ok": True, "ignored": event}
