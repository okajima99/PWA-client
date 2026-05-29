"""プロセス内で共有する状態 (シングルプロセス FastAPI 前提)。

`session_id` (= UI 上の 1 セッション = 1 議題) を一意キーとして、 全状態を保持する。
セッションは作成時に `agent_id` (config.json AGENTS の key) を 1 つ持ち、
それによって cwd / 通知タイトル既定値などの定義を引く。 同じ agent_id を持つ
セッションは複数同時に存在できる (= 同じ作業ディレクトリで複数議題を並行で持てる)。

- セッション定義 (`sessions_meta`): 永続化、 session_meta.json
- ストリームごとの状態 (`stream_states`)
- ステータスキャッシュ (`agent_status`, `shared_status`)
- claude セッション ID の永続化 (`sessions` + `save_sessions`): session_id → claude session_id

異なるモジュールから書き換えたい値は dict や dataclass にラップして
import 越しに mutate できる形にしている。
"""
import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from config import AGENTS

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, content: str) -> None:
    """tmp ファイルに書いて os.replace で差し替える atomic write。
    書き込み途中に kill されても元ファイルは壊れない。 同一 FS 内のみ atomic。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)

# --- 永続化パス ---
SESSIONS_PATH = Path(__file__).parent / "sessions.json"
SESSION_META_PATH = Path(__file__).parent / "session_meta.json"

# SDK が ResultMessage.model_usage で contextWindow を返してくれない / agent_status にもまだ
# 入ってない初回の fallback 値。 Sonnet / Opus の最大コンテキスト相当 (= 1M tokens)。
# usage.py からも参照されるが、 依存方向は usage → state に固定する (= state は usage を import しない)
# ことで module init 時の循環 import を回避する。
DEFAULT_CTX_WINDOW = 1_000_000


# --- セッション定義 (= UI 上の 1 タブ) ---
@dataclass
class SessionDef:
    id: str
    agent_id: str
    title: str
    created_at: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "title": self.title,
            "created_at": self.created_at,
        }


def _default_title(agent_id: str, index: int) -> str:
    cfg = AGENTS.get(agent_id) or {}
    base = cfg.get("display_name") or agent_id.upper()
    return f"{base}-{index}"


def _new_session_id() -> str:
    return f"ses_{uuid.uuid4().hex[:12]}"


def _load_sessions_meta_and_claude_sessions() -> tuple[dict[str, SessionDef], dict[str, str | None]]:
    """session_meta.json + sessions.json をロード。 旧 sessions.json (agent_id キー) を
    検出した場合は agent ごと 1 セッションをマイグレーションして両ファイルを書き換える。
    """
    meta_raw: list[dict] | None = None
    sessions_raw: dict | None = None

    if SESSION_META_PATH.exists():
        try:
            meta_raw = json.loads(SESSION_META_PATH.read_text())
        except Exception:
            meta_raw = None
    if SESSIONS_PATH.exists():
        try:
            sessions_raw = json.loads(SESSIONS_PATH.read_text())
        except Exception:
            sessions_raw = None

    sessions_meta: dict[str, SessionDef] = {}
    claude_sessions: dict[str, str | None] = {}

    if isinstance(meta_raw, list):
        # 通常パス: session_meta.json に従う (空配列でもこちらに通す = 0 セッション起動 OK)
        for entry in meta_raw:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("id")
            aid = entry.get("agent_id")
            title = entry.get("title") or aid or "session"
            created = entry.get("created_at") or int(time.time())
            if not sid or aid not in AGENTS:
                # agent_id が config から消えてる (= 過去 session のまま config 更新で消失)、
                # その session は UI に出せないので skip。 観測のため warn を残す。
                if sid:
                    logger.warning("session %s skipped: agent_id %r not in AGENTS", sid, aid)
                continue
            sessions_meta[sid] = SessionDef(
                id=sid, agent_id=aid, title=title, created_at=int(created)
            )
        if isinstance(sessions_raw, dict):
            # 後方互換マップ: sessions.json が旧形式 (agent_id キー) のままだった場合、
            # session_meta の各 entry の agent_id をキーに引いて claude session_id を救出する。
            # 同 agent_id を持つ session_meta entry が 2 つ以上ある場合は最初の 1 つだけ拾う
            # (重複は事実上発生しない、 マイグレーション直後のみ意味を持つ)。
            legacy_consumed: set[str] = set()
            for sid, meta in sessions_meta.items():
                v = sessions_raw.get(sid)
                if isinstance(v, str):
                    claude_sessions[sid] = v
                    continue
                aid = meta.agent_id
                if aid in sessions_raw and aid not in legacy_consumed:
                    legacy_v = sessions_raw.get(aid)
                    if isinstance(legacy_v, str):
                        claude_sessions[sid] = legacy_v
                        legacy_consumed.add(aid)
                        continue
                claude_sessions[sid] = None
            # 救出が走ったら新形式で書き戻す (次回以降の loader は通常パスで済む)
            if legacy_consumed:
                _persist_sessions(claude_sessions)
        else:
            for sid in sessions_meta:
                claude_sessions[sid] = None
    else:
        # マイグレーション or 初期化: agent ごと 1 セッションを生成する
        legacy = sessions_raw if isinstance(sessions_raw, dict) else {}
        per_agent_idx: dict[str, int] = {}
        now = int(time.time())
        for agent_id in AGENTS:
            sid = _new_session_id()
            per_agent_idx[agent_id] = per_agent_idx.get(agent_id, 0) + 1
            sessions_meta[sid] = SessionDef(
                id=sid,
                agent_id=agent_id,
                title=_default_title(agent_id, per_agent_idx[agent_id]),
                created_at=now,
            )
            v = legacy.get(agent_id)
            claude_sessions[sid] = v if isinstance(v, str) else None
        # 永続化 (起動時 1 回のみ)
        _persist_meta(sessions_meta)
        _persist_sessions(claude_sessions)

    return sessions_meta, claude_sessions


def _persist_meta(meta: dict[str, SessionDef]) -> None:
    atomic_write_text(
        SESSION_META_PATH,
        json.dumps(
            [m.to_dict() for m in meta.values()],
            ensure_ascii=False,
            indent=2,
        ),
    )


def _persist_sessions(claude_sessions: dict[str, str | None]) -> None:
    atomic_write_text(SESSIONS_PATH, json.dumps(claude_sessions, ensure_ascii=False))


def save_sessions_meta() -> None:
    _persist_meta(sessions_meta)


def save_sessions() -> None:
    _persist_sessions(sessions)


sessions_meta, sessions = _load_sessions_meta_and_claude_sessions()


# --- ストリーム状態 ---
@dataclass
class StreamState:
    agent_id: str = ""  # どの AGENTS 設定 (cwd / notification_title) を参照するか
    # buffer / buffer_id / complete は /status payload (_build_status) が読む。
    # PTY + JSONL 経路では buffer に積まれないが、 status の streaming フラグ /
    # buffer_id 整合のためフィールドだけ維持する。
    buffer: list[str] = field(default_factory=list)
    buffer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    complete: bool = True
    # AskUserQuestion の回答待ち tool_use id (= /status payload に載せる)。
    pending_question_tool_id: str | None = None
    # session 別 model / effort 上書き。 None なら AGENTS 設定 + env デフォルトを使う。
    # PATCH /sessions/{id}/config で更新 → claude TUI に /model /effort を send-keys。
    model_override: str | None = None
    effort_override: str | None = None  # "low" | "medium" | "high"
    # 状態変化シグナル (= /status/{sid}/stream SSE が wait する event)。
    # current_tool 変化 / todos 更新等 (= hooks / jsonl 経路) で set、 SSE 受信側は
    # 現状 status JSON を yield して event.clear() する。 backend→frontend を即時 push。
    status_event: asyncio.Event = field(default_factory=asyncio.Event)


def _make_agent_status(agent_id: str) -> dict:
    cfg = AGENTS.get(agent_id) or {}
    return {
        "ctx_pct": 0,
        "ctx_window": DEFAULT_CTX_WINDOW,
        "model": cfg.get("model", ""),
        "plan_mode": False,
        "current_tool": None,
        "todos": None,
        "subagent": None,
        # ExitPlanMode の承認待ち情報。 tool_use 発火で set / tool_result で clear。
        # frontend が PlanApprovalBubble を表示するためのソース。
        # {tool_use_id: str, plan: str, choices: [{key: str, label: str}, ...]} または None
        "pending_plan": None,
        # AskUserQuestion のライブ表示用。 claude は AskUserQuestion で停止中、 会話ログ
        # (JSONL) を回答までディスクに flush しないので、 JSONL tail では質問をライブ検出
        # できない。 そこで PreToolUse hook (= 質問表示時にリアルタイム発火) で立て、
        # 回答後 flush の JSONL tool_result で clear する。 tool_use_id は hook payload に
        # 無いので None で立て、 JSONL の AskUserQuestion tool_use 行で補完する。
        # {tool_use_id: str|None, questions: [...]} または None
        "pending_question": None,
    }


stream_states: dict[str, StreamState] = {
    sid: StreamState(agent_id=meta.agent_id) for sid, meta in sessions_meta.items()
}

# --- セッションごとの一時ファイル ---
session_tmp_files: dict[str, list[Path]] = {}

# --- ステータスキャッシュ ---
shared_status: dict = {
    "five_hour_pct": 0,
    "seven_day_pct": 0,
    "five_hour_resets_at": 0,
    "seven_day_resets_at": 0,
}

agent_status: dict[str, dict] = {
    sid: _make_agent_status(meta.agent_id) for sid, meta in sessions_meta.items()
}

# backend プロセスの起動時刻 (= /status payload に含めて frontend が再起動を検知)。
# LaunchAgent KeepAlive で自動再起動した場合に、 frontend 側で stale な streaming bubble を
# 停止扱いに固定するためのシグナル。
backend_start_time: float = time.time()


# --- セッション操作ヘルパ ---
def register_session(agent_id: str, title: str | None = None) -> SessionDef:
    """新規セッションを登録して全状態 dict を初期化する。 永続化まで行う。"""
    if agent_id not in AGENTS:
        raise ValueError(f"Unknown agent_id: {agent_id}")
    sid = _new_session_id()
    if not title:
        existing_count = sum(1 for m in sessions_meta.values() if m.agent_id == agent_id)
        title = _default_title(agent_id, existing_count + 1)
    meta = SessionDef(
        id=sid, agent_id=agent_id, title=title, created_at=int(time.time())
    )
    sessions_meta[sid] = meta
    stream_states[sid] = StreamState(agent_id=agent_id)
    agent_status[sid] = _make_agent_status(agent_id)
    sessions[sid] = None
    save_sessions_meta()
    save_sessions()
    return meta


def unregister_session(session_id: str) -> bool:
    """セッションを完全削除。 PTY / tmux の停止は呼び出し側責任。"""
    if session_id not in sessions_meta:
        return False
    sessions_meta.pop(session_id, None)
    stream_states.pop(session_id, None)
    agent_status.pop(session_id, None)
    sessions.pop(session_id, None)
    session_tmp_files.pop(session_id, None)
    save_sessions_meta()
    save_sessions()
    return True


def rename_session(session_id: str, title: str) -> bool:
    if session_id not in sessions_meta or not title:
        return False
    sessions_meta[session_id].title = title
    save_sessions_meta()
    return True


# SDK レスポンス / HTTP header の解析と agent_status / shared_status の更新は
# `usage.py` に分離した (2026-05-17)。 state.py は純粋に state の定義 / lifecycle に専念。
