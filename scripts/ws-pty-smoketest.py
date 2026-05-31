#!/usr/bin/env python3
"""`/ws/pty/{session_id}` の最小 smoketest client。

backend に対して WebSocket 接続して PTY 経由 claude を 1 turn 走らせ、 出力を
そのまま stdout に流す。 手動で動作確認したい時に使う。

使い方:
    # backend 起動済の状態で:
    python3 scripts/ws-pty-smoketest.py
    python3 scripts/ws-pty-smoketest.py ws://localhost:8000/ws/pty/smoke
    python3 scripts/ws-pty-smoketest.py wss://<host>.tail<xxxx>.ts.net/ws/pty/smoke

出力は xterm.js が render する想定の ANSI バイト列がそのまま流れる。 ターミナルで
読む時は ANSI が効くのでざっくり読める (= 色 / cursor 制御も発火する)。 終了は
`Ctrl-D` (= EOF を stdin 経由で送る) or `Ctrl-C` (= 接続切断)。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

DEFAULT_URL = os.environ.get("WS_PTY_URL", "ws://localhost:8000/ws/pty/smoketest")


async def _pump_stdin_to_ws(ws) -> None:
    """ローカル stdin → WebSocket バイナリ frame。 EOF で抜ける。"""
    loop = asyncio.get_event_loop()
    while True:
        # blocking read を別 thread に逃がす (= asyncio main loop を塞がない)
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:  # EOF
            return
        await ws.send(line)


async def _pump_ws_to_stdout(ws) -> None:
    """WebSocket バイナリ frame → ローカル stdout。 text frame は JSON 制御として解釈。"""
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                sys.stdout.buffer.write(msg)
                sys.stdout.buffer.flush()
            else:
                try:
                    ctrl = json.loads(msg)
                except json.JSONDecodeError:
                    sys.stderr.write(f"[ctrl ?] {msg}\n")
                    continue
                if ctrl.get("type") == "exit":
                    sys.stderr.write(
                        f"\n[backend reports PTY exited rc={ctrl.get('returncode')}]\n"
                    )
                    return
                sys.stderr.write(f"[ctrl] {ctrl}\n")
    except websockets.ConnectionClosed:
        return


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    sys.stderr.write(f"connecting to {url}\n")
    try:
        async with websockets.connect(url, max_size=2**22) as ws:
            sys.stderr.write("connected. type into stdin, EOF / Ctrl-D to exit.\n")
            # 初期 resize を 1 個送って claude の TUI を画面幅に合わせる
            await ws.send(json.dumps({"type": "resize", "rows": 40, "cols": 120}))
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(_pump_stdin_to_ws(ws)),
                    asyncio.create_task(_pump_ws_to_stdout(ws)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake) as e:
        sys.stderr.write(f"connection failed: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
