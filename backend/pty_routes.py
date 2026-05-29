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

from fastapi import APIRouter, Body, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from chat_content import save_to_tmp
from config import AGENTS
import re

from pty_runner import (
    PtySession,
    has_tmux_session,
    jsonl_path_for_session,
    pty_sessions,
    resize_pty,
    spawn_pty_session,
    tmux_send_keys,
    write_pty,
)
from state import sessions_meta


# 素プロンプト (= ユーザ発言の user 行) 判定用の harness XML プレフィックス。
# /clear や local-command-* の内部表現は ユーザ発言ではないので除外する。
_HARNESS_RE = re.compile(
    r"^\s*<(command-name|command-message|command-args|local-command-[a-z-]+)\b"
)


def _count_user_prompts(path) -> int:
    """JSONL から素プロンプト (= 実ユーザ発言) の user 行数を数える。
    tool_result / isMeta / isSidechain / harness XML は除外。 送信確認に使う。"""
    if not path:
        return 0
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0
    count = 0
    for raw in data.split(b"\n"):
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            s = c.strip()
            if s and not _HARNESS_RE.match(s):
                count += 1
        elif isinstance(c, list):
            texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if any((t or "").strip() for t in texts):
                count += 1
    return count


async def _wait_user_prompt_added(path, initial_count: int, timeout: float) -> bool:
    """JSONL の user 行が initial_count から増えるのを timeout 秒まで poll する。"""
    poll = 0.1
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _count_user_prompts(path) > initial_count:
            return True
        await asyncio.sleep(poll)
    return _count_user_prompts(path) > initial_count


def _resolve_cwd(session_id: str) -> str | None:
    """session_id から起動 cwd を解決する。

    優先順:
        1. session_id がそのまま AGENTS の key (= 直リンク `?terminal=agent_a` 等)
        2. session_id が sessions_meta に登録済なら、 そこに紐付く agent_id 経由で
           AGENTS から取得 (= UI でセッションタブを作る通常経路)
        3. どちらも該当なし → None (= backend の起動 cwd で zsh が立ち上がる)
    """
    cfg = _resolve_agent_cfg(session_id)
    return cfg.get("cwd") if cfg else None


def _resolve_agent_cfg(session_id: str) -> dict | None:
    """session_id から AGENTS の cfg dict を解決する (= cwd と launch_alias の共通解決)。"""
    cfg = AGENTS.get(session_id)
    if cfg:
        return cfg
    meta = sessions_meta.get(session_id)
    if meta is not None:
        return AGENTS.get(meta.agent_id)
    return None


async def ensure_pty_session_for(session_id: str) -> None:
    """指定 session の tmux + claude を起動 (既にあれば何もしない)。

    `/ws/pty/{sid}` (= ターミナル画面) 経由だけでなく、 `/jsonl/stream/{sid}`
    (= チャット画面) からも呼ぶことで、 ターミナル画面を一度も開いていないタブでも
    claude が立ち上がって JSONL が作られるようにする。
    """
    existing = pty_sessions.get(session_id)
    if existing is not None and not existing.exit_event.is_set():
        return
    if has_tmux_session(session_id):
        # tmux session は生きてるが backend 側に PtySession 記録が無い (= backend 再起動跨ぎ)。
        # チャット画面側からは attach の必要なし。 JSONL は claude プロセスが書き続けてるので
        # 解決経路 (= jsonl_path_for_session) が拾える。 spawn 重複も避ける
        return
    cfg = _resolve_agent_cfg(session_id) or {}
    cwd = cfg.get("cwd")
    launch_alias = cfg.get("launch_alias")
    try:
        session = await spawn_pty_session(
            session_id, cwd=cwd, launch_alias=launch_alias,
        )
    except Exception:
        logger.exception("ensure_pty_session_for: spawn failed session=%s", session_id)
        return
    pty_sessions[session_id] = session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/pty/{session_id}")
async def pty_socket(ws: WebSocket, session_id: str) -> None:
    await ws.accept()

    # scrollback の自動復元は無効化 (= 2026-05-21 再試行で描画破綻、 旧症状再発)。
    # capture-pane の history を流すと、 中に含まれる ANSI cursor 制御 (= claude
    # streaming 中の途中再描画指示等) が新接続側の状態と整合せず画面が壊れる。

    session = pty_sessions.get(session_id)
    if session is None or session.exit_event.is_set():
        cfg = _resolve_agent_cfg(session_id) or {}
        cwd = cfg.get("cwd")
        launch_alias = cfg.get("launch_alias")
        try:
            session = await spawn_pty_session(
                session_id, cwd=cwd, launch_alias=launch_alias,
            )
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
            # client が既に切断済なら静かに終わる (= 閉じた WS への send を試みない)。
            if ws.client_state != WebSocketState.CONNECTED:
                return
            if session.exit_event.is_set() and session.output_queue.empty():
                # 子終了通知を 1 度だけ送って終わる
                try:
                    await ws.send_text(json.dumps({
                        "type": "exit",
                        "returncode": session.process.returncode,
                    }))
                except (WebSocketDisconnect, RuntimeError):
                    pass
                return
            try:
                data = await asyncio.wait_for(session.output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await ws.send_bytes(data)
    except WebSocketDisconnect:
        return
    except RuntimeError as e:
        # WS が閉じた後の send は starlette が "Unexpected ASGI message 'websocket.send'"
        # の RuntimeError を投げる。 異常ではなく client 切断の一種なので、 exception ログ
        # ではなく debug で静かに終える (= 2026-05-28 に 8 回以上ログを噴いた汚染源)。
        logger.debug("_pump_to_client: ws closed mid-send session=%s: %s", session.session_id, e)
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


@router.post("/pty/{session_id}/send")
async def pty_send(session_id: str, payload: dict = Body(...)) -> dict:
    """chat UI からの入力を tmux session に送る (= send-keys 経路、 PTY attach 不要)。

    送信本文 (= text + enter) の場合は、 JSONL に user 行が +1 されるかを最大 2s
    監視して機械的に送信成功を確認する。 +1 されなければ 1 回だけ再送して +1.5s 待つ。
    確認できなければ ok=False で返し、 frontend に「届かなかった」 ことを通知する
    (= メッセージボックスに text を残して再送できるようにする経路)。

    payload:
        text  (str, optional): literal 文字列 (= プロンプト本文)
        key   (str, optional): tmux キー名 (= "Escape" で停止、 "C-c" 等)
        enter (bool, optional): 末尾に Enter (= 確定)
    """
    text = payload.get("text")
    key = payload.get("key")
    enter = bool(payload.get("enter", False))
    # 確認対象は「ユーザ送信本文」 = text あり + enter ありのケースのみ。
    # 自由記述以外のキー送信 (Escape 等)、 AskUserQuestion 自由記述の 1 回目 (typeNum、 enter なし)
    # 等は確認しない (= 送信完了の概念がない、 or 別経路で確認)。
    confirm = bool(text) and enter
    initial_count = 0
    jsonl_path = None
    if confirm:
        jsonl_path = jsonl_path_for_session(session_id)
        if jsonl_path is not None:
            initial_count = _count_user_prompts(jsonl_path)
    ok = tmux_send_keys(session_id, text=text, key=key, enter=enter)
    if not ok or not confirm or jsonl_path is None:
        return {"ok": ok}
    # 第 1 回 確認待ち (= 監視窓 4s。 launch_alias 起動直後や重い paste 展開で JSONL flush が
    # 遅れるケースを取りこぼさないよう 2s から延長、 2 段リトライ前の取り逃しを減らす)。
    if await _wait_user_prompt_added(jsonl_path, initial_count, timeout=4.0):
        return {"ok": True, "confirmed": True}
    # 再送 #1: paste は既に TUI に届いている可能性が高いので Enter だけ追い打ち
    # (= claude TUI の `paste again to expand` プレースホルダ展開 + 送信 で Enter 1 個が
    # 吸われたケースを救済する。 paste を再度送ると重複 paste / 状態破壊のリスクがあるため
    # まず Enter だけで試す)。
    logger.warning(
        "pty_send: no user prompt within 2s, retrying with Enter only: sid=%s text_len=%d",
        session_id, len(text or ""),
    )
    tmux_send_keys(session_id, enter=True)
    if await _wait_user_prompt_added(jsonl_path, initial_count, timeout=1.0):
        return {"ok": True, "confirmed": True, "retried": "enter_only"}
    # 再送 #2: それでもダメなら paste + Enter フル再送
    logger.warning("pty_send: enter-only retry failed, full re-paste: sid=%s", session_id)
    tmux_send_keys(session_id, text=text, enter=True)
    if await _wait_user_prompt_added(jsonl_path, initial_count, timeout=1.5):
        return {"ok": True, "confirmed": True, "retried": "full"}
    logger.warning("pty_send: failed even after both retries: sid=%s", session_id)
    return {"ok": False, "reason": "no_user_prompt_recorded"}


@router.post("/pty/{session_id}/send-with-files")
async def pty_send_with_files(
    session_id: str,
    text: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    """添付ファイル付きで text を tmux session に送る。 file は uploads/tmp に保存して
    保存先 path を本文末尾に追記する形で claude に投入する (= claude が Read tool で
    自分で読む経路、 旧 SDK 経路の base64 image 同梱と違って tmux 打鍵が軽い)。

    payload (multipart/form-data):
        text  (str):              本文
        files (list[UploadFile]): 添付ファイル群 (画像 / テキスト / その他何でも)
    """
    saved = await save_to_tmp(files, session_id)
    parts: list[str] = []
    if text.strip():
        parts.append(text.strip())
    if saved:
        # 改行込みの本文を tmux send-keys に渡すと claude の入力欄で意図せぬ確定が起きうるので
        # 1 行に押し込む (= 「[添付ファイル: /path/to/a, /path/to/b]」)。 path に空白は入らない
        # 前提 (= chat_content.save_to_tmp が uuid.hex + 元拡張子で命名するので安全)。
        paths = ", ".join(s["path"] for s in saved)
        parts.append(f"[添付ファイル: {paths}]")
    full_text = " ".join(parts)
    if not full_text:
        return {"ok": False, "reason": "empty"}
    ok = tmux_send_keys(session_id, text=full_text, enter=True)
    return {
        "ok": ok,
        "saved_files": [{"name": s["name"], "path": s["path"]} for s in saved],
    }


