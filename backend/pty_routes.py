"""WebSocket endpoint for the PTY runner (= phase 1 PTY 経路、 旧 SDK 経路と共存)。

Wire protocol (= xterm.js に直接食わせる前提):
    Server → Client:
        - binary frame: PTY 子プロセスからの raw stdout バイト列。
          そのまま xterm.write() に渡すと ANSI 含めて render される。
    Client → Server:
        - binary frame: user 入力 (= stdin に流すバイト列、 keystroke そのまま)。
        - text frame (JSON): control message。
            {"type": "resize", "rows": <int>, "cols": <int>}

接続契機:
    - 新規 session_id: claude プロセスを spawn して PtySession を作る
    - 既存 session_id (= 生存中): 既存セッションに再アタッチ、 過去出力は queue 残量分が即流れる
    - 既存 session_id (= exit 済): 新規 spawn し直し
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config import AGENTS, USE_PTY_RUNNER
from pty_runner import (
    PtySession,
    capture_tmux_scrollback,
    pty_sessions,
    resize_pty,
    spawn_pty_session,
    write_pty,
)
from state import sessions_meta


def _resolve_cwd(session_id: str) -> str | None:
    """session_id から起動 cwd を解決する。

    優先順:
        1. session_id がそのまま AGENTS の key (= 直リンク `?terminal=agent_a` 等)
        2. session_id が sessions_meta に登録済なら、 そこに紐付く agent_id 経由で
           AGENTS から取得 (= UI でセッションタブを作る通常経路)
        3. どちらも該当なし → None (= backend の起動 cwd で zsh が立ち上がる)
    """
    cfg = AGENTS.get(session_id)
    if cfg:
        return cfg.get("cwd")
    meta = sessions_meta.get(session_id)
    if meta is not None:
        agent_cfg = AGENTS.get(meta.agent_id)
        if agent_cfg:
            return agent_cfg.get("cwd")
    return None

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/pty/{session_id}")
async def pty_socket(ws: WebSocket, session_id: str) -> None:
    if not USE_PTY_RUNNER:
        # 接続前に閉じる (= accept しない、 4xx 系の close code で意図を伝える)
        await ws.close(code=4001, reason="USE_PTY_RUNNER is false")
        return
    await ws.accept()

    # scrollback の自動復元は無効化 (= 2026-05-21 再試行で描画破綻、 旧症状再発)。
    # capture-pane の history を流すと、 中に含まれる ANSI cursor 制御 (= claude
    # streaming 中の途中再描画指示等) が新接続側の状態と整合せず画面が壊れる。

    session = pty_sessions.get(session_id)
    if session is None or session.exit_event.is_set():
        cwd = _resolve_cwd(session_id)
        try:
            session = await spawn_pty_session(session_id, cwd=cwd)
        except Exception as e:
            logger.exception("PTY spawn failed session=%s", session_id)
            try:
                await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
            finally:
                await ws.close(code=4002, reason="spawn failed")
            return
        pty_sessions[session_id] = session

    pump_out = asyncio.create_task(_pump_to_client(ws, session))
    pump_in = asyncio.create_task(_pump_from_client(ws, session))

    done, pending = await asyncio.wait(
        [pump_out, pump_in],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    # 子プロセスは閉じない (= 再接続できるよう生かしておく、 idle GC は別途)
    try:
        await ws.close()
    except Exception:
        pass


async def _pump_to_client(ws: WebSocket, session: PtySession) -> None:
    """PTY 出力 queue → client へバイナリで流す。"""
    try:
        while True:
            if session.exit_event.is_set() and session.output_queue.empty():
                # 子終了通知を 1 度だけ送って終わる
                try:
                    await ws.send_text(json.dumps({
                        "type": "exit",
                        "returncode": session.process.returncode,
                    }))
                except Exception:
                    pass
                return
            try:
                data = await asyncio.wait_for(session.output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await ws.send_bytes(data)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("_pump_to_client error session=%s", session.session_id)


async def _pump_from_client(ws: WebSocket, session: PtySession) -> None:
    """client 入力 → PTY stdin / control。"""
    try:
        while True:
            msg = await ws.receive()
            # FastAPI WebSocket は dict で {"type": "websocket.disconnect" | "websocket.receive", ...}
            if msg.get("type") == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data:
                write_pty(session, data)
                continue
            text = msg.get("text")
            if text:
                try:
                    ctrl = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if ctrl.get("type") == "resize":
                    resize_pty(
                        session,
                        int(ctrl.get("rows", 40)),
                        int(ctrl.get("cols", 120)),
                    )
                elif ctrl.get("type") == "input":
                    # debug / fallback 経路 (= バイナリが使えない client 用)
                    payload = ctrl.get("data", "")
                    if isinstance(payload, str):
                        write_pty(session, payload.encode("utf-8"))
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("_pump_from_client error session=%s", session.session_id)


@router.get("/api/agents")
def list_agents() -> dict:
    """session picker 用に AGENTS のサマリを返す。

    cwd 等の path を露出するのは tailnet 内に限定された運用前提なので OK、 でも
    必要最小限に絞る (= display_name と id だけ + plain shell 用の擬似 entry)。
    """
    agents = [
        {
            "id": agent_id,
            "display_name": cfg.get("display_name") or agent_id,
        }
        for agent_id, cfg in AGENTS.items()
    ]
    # AGENTS に紐付かない素の terminal session 用エントリも明示的に出す。
    # session_id="shell" は backend 側で AGENTS lookup を miss して default cwd で
    # zsh を起動する経路 (= 既存の spawn 経路の素直な動作)。
    agents.append({"id": "shell", "display_name": "Plain shell"})
    return {"agents": agents}
