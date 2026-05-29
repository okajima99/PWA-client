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
    を逆引きして、 PWA タブ識別子 (= `ses_xxxx`) を返す。

    逆引き元は `jsonl_watcher` の confirmed bindings。 これは SessionStart hook 経由で
    X-PWA-SID header 付き curl が来た時のみ書き込まれる確定経路で、 Desktop App /
    ターミナル直叩きの claude は PWA_SID env を持たず SessionStart で弾かれるため
    bindings に入らない (= 構造的に紐付け不可)。 旧実装の statusline map 逆引きは
    map ファイルが古い claude_sid を保持していて誤マッチを起こすケースが実機で確認
    された (= 2026-05-28 ログで Desktop App resume の claude_sid が PWA タブに誤紐付け)
    ため廃止する。

    マッチしなければ None (= Web Push 抑制)。
    """
    if not claude_sid:
        return None
    try:
        import jsonl_watcher  # noqa: PLC0415
        bindings = jsonl_watcher.list_bindings()
    except Exception:
        logger.exception("_pwa_session_for_claude_sid: list_bindings failed")
        return None
    for pwa_sid, info in bindings.items():
        if not info or not info.get("confirmed"):
            continue
        jsonl_path = info.get("jsonl_path")
        if not jsonl_path:
            continue
        # jsonl_path のファイル名 stem (= 拡張子前) が claude_sid と一致するか。
        # claude CLI の JSONL ファイル名は `<claude_session_id>.jsonl` 規約。
        if Path(jsonl_path).stem == claude_sid:
            return pwa_sid
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

    # 確定 binding (= 全イベント共通)。 claude の hook は **どのイベントでも** payload に
    # transcript_path を載せ、 PWA spawn が tmux env に注入した PWA_SID から X-PWA-SID
    # header が全イベントに付く (= PWA タブの claude だけがここを通る、 Desktop / ターミナル
    # 直叩きは header 無し)。 よって「そのタブの claude 自身が報告した transcript」 という
    # 100% 確定の事実だけで pwa_sid → jsonl を毎回確定できる。 backend 再起動後も最初の
    # hook 1 発で正しい jsonl に自己修復するので、 birthtime / cwd による確率紐付けは不要。
    pwa_sid_hdr = request.headers.get("x-pwa-sid", "").strip()
    transcript = payload.get("transcript_path")
    if pwa_sid_hdr and transcript:
        import jsonl_watcher  # noqa: PLC0415
        jsonl_watcher.confirm_bind(pwa_sid_hdr, claude_sid or "", transcript)

    # SessionStart: PWA タブ起動時に発火する確定 binding 経路。 PWA spawn 時に tmux
    # session env に `PWA_SID=ses_xxx` を注入してるので、 PWA タブで起動した claude が
    # 呼ぶ hook だけ X-PWA-SID header を持つ (= Desktop App / ターミナル直叩きの claude は
    # PWA_SID env が無いので header が空 → ここで弾かれる)。 /clear で source=clear で
    # 再発火するので新 claude_sid に自動追従する。
    # PreToolUse(AskUserQuestion): 質問が表示された瞬間にリアルタイム発火する
    # (= JSONL は回答まで flush されないので tail では検出不可、 実測で確認済み)。
    # ここで pending_question を立てて status SSE 経由で frontend にライブ表示させる。
    # PWA 判定は X-PWA-SID header (= statusline map 逆引きに依存しない確定経路、
    # SessionStart と同じ)。 tool_use_id は payload に無いので None で立て、 回答後 flush
    # の JSONL AskUserQuestion tool_use 行から補完する (= jsonl_routes._mutate_agent_status)。
    if event == "PreToolUse":
        pwa_sid_hdr = request.headers.get("x-pwa-sid", "").strip()
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input") or {}
        if tool_name == "AskUserQuestion" and pwa_sid_hdr:
            questions = tool_input.get("questions") or []
            a = agent_status.get(pwa_sid_hdr)
            if a is not None and questions:
                a["pending_question"] = {"tool_use_id": None, "questions": questions}
                st = stream_states.get(pwa_sid_hdr)
                if st is not None:
                    st.status_event.set()
                logger.info(
                    "PreToolUse AskUserQuestion → pending_question set: pwa_sid=%s nq=%d",
                    pwa_sid_hdr, len(questions),
                )
        return {"ok": True, "observed": tool_name}

    if event == "SessionStart":
        pwa_sid_hdr = request.headers.get("x-pwa-sid", "").strip()
        transcript = payload.get("transcript_path")
        source = payload.get("source")
        if not pwa_sid_hdr:
            logger.info(
                "SessionStart ignored (no PWA_SID): claude_sid=%s source=%s cwd=%s",
                claude_sid, source, cwd,
            )
            return {"ok": True, "ignored": "non_pwa_session"}
        if not transcript:
            logger.warning(
                "SessionStart missing transcript_path: pwa_sid=%s claude_sid=%s",
                pwa_sid_hdr, claude_sid,
            )
            return {"ok": False, "reason": "no_transcript_path"}
        import jsonl_watcher  # noqa: PLC0415
        path = jsonl_watcher.confirm_bind(pwa_sid_hdr, claude_sid or "", transcript)
        logger.info(
            "SessionStart bound: pwa_sid=%s source=%s claude_sid=%s -> %s",
            pwa_sid_hdr, source, claude_sid, path.name if path else None,
        )
        return {"ok": path is not None, "pwa_sid": pwa_sid_hdr,
                "bound": str(path) if path else None}

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
        # claude が permission prompt 等で発火する generic な「待ち」 通知は skip。
        # 中身が "Claude is waiting for your input" 系の固定文の時は知らせても情報量ゼロ、
        # AskUserQuestion の質問本文等のシグナルとは別物。
        msg_lower = message.lower()
        if "is waiting" in msg_lower or "needs your input" in msg_lower:
            return {"ok": True, "ignored": "generic_waiting"}
        body = _truncate(message)
        asyncio.create_task(broadcast_push(body, title, pwa_session_id))
        return {"ok": True, "pushed": "Notification"}

    # 未対応イベントは受信ログだけ残して通す
    return {"ok": True, "ignored": event}
