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
    pty_sessions,
    resize_pty,
    spawn_pty_session,
    write_pty,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/pty/{session_id}")
async def pty_socket(ws: WebSocket, session_id: str) -> None:
    if not USE_PTY_RUNNER:
        # 接続前に閉じる (= accept しない、 4xx 系の close code で意図を伝える)
        await ws.close(code=4001, reason="USE_PTY_RUNNER is false")
        return
    await ws.accept()

    # scrollback の自動復元は無効化中: 接続元 client の幅 (= 例 iPhone Safari 30 cols)
    # と capture 時の pane 幅 (= 例 Mac Chrome 120 cols) がズレてると、 ANSI の絶対カーソル
    # 位置指定が崩れて TUI が左右に分裂表示される。 正しく直すには「client の resize 受信
    # → tmux pane を新幅に refresh → capture-pane → 送信」 の順序にする必要があり、
    # 現状の「接続即送信」 とは経路が違うので別 PR で。 当面は live 出力のみで運用。
    # 必要なら capture_tmux_scrollback() / has_tmux_session() は他経路 (= 明示要求の
    # WS message 等) から呼べる。

    session = pty_sessions.get(session_id)
    if session is None or session.exit_event.is_set():
        cwd = None
        if session_id in AGENTS:
            cwd = AGENTS[session_id].get("cwd")
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
