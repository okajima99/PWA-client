"""使用率系の状態 (= 5h/7d/ctx/model) を組み立てる層。

rate-limits.jsonl (= statusline 記録) の読み取りと、 ResultMessage.usage からの
context 使用率計算を担当し、 agent_status を更新する。 state.py は純粋な state 定義・
lifecycle に専念し、 「使用率の計算」 はここに集約する (= 2026-05-17 責務分離)。
"""
import json

from config import RATE_LIMITS_LOG_PATH
from state import DEFAULT_CTX_WINDOW, agent_status


def read_latest_rate_limits() -> dict:
    """rate-limits.jsonl (= statusline が記録) の最終行から 5h/7d/ctx/model を読む。

    proxy を一切使わず、 claude CLI 自身が statusline subprocess に渡す使用率を
    ファイル経由で拾う。 ファイル末尾の数 KB だけ読んで最終行を取る (= 大きくても軽い)。
    値が取れなければ空 dict (= 呼び出し側は既存 shared_status を維持)。
    """
    if not RATE_LIMITS_LOG_PATH:
        return {}
    try:
        with open(RATE_LIMITS_LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return {}
    try:
        last = json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return {
        "five_hour_pct": last.get("five_hour_pct"),
        "seven_day_pct": last.get("seven_day_pct"),
        "five_hour_resets_at": last.get("five_hour_resets_at"),
        "seven_day_resets_at": last.get("seven_day_resets_at"),
        "context_pct": last.get("context_pct"),
        "model": last.get("model"),
    }


def compute_ctx_pct(usage: dict, ctx_window: int = DEFAULT_CTX_WINDOW) -> int:
    """AssistantMessage.usage 辞書から context 使用率 % を計算。"""
    if not usage or ctx_window <= 0:
        return 0
    total = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    return min(round(total / ctx_window * 100), 100)


def format_model_name(key: str) -> str:
    """ResultMessage.model_usage キー (= "claude-opus-4-1-...") を「Opus 4.1.…」 形式に。"""
    key = key.replace("claude-", "")
    parts = key.split("-")
    if len(parts) >= 3:
        name = parts[0].capitalize()
        version = ".".join(parts[1:])
        return f"{name} {version}"
    return key.capitalize()


def update_agent_from_result(session_id: str, model_usage: dict | None, last_assistant_usage: dict | None) -> None:
    """ResultMessage.model_usage と直近 AssistantMessage.usage から model / ctx_window /
    ctx_pct を更新する。 SDK 経由でしか取れない正確な ctx_window をここで持ち越す。"""
    if not model_usage or session_id not in agent_status:
        return
    model_key = next(iter(model_usage), None)
    if not model_key:
        return
    agent_status[session_id]["model"] = format_model_name(model_key)
    # ctx_window 解決優先順: ResultMessage の正確値 → agent_status 前回値 → default。
    ctx_window = (
        model_usage[model_key].get("contextWindow")
        or agent_status[session_id].get("ctx_window")
        or DEFAULT_CTX_WINDOW
    )
    agent_status[session_id]["ctx_window"] = ctx_window
    if last_assistant_usage:
        agent_status[session_id]["ctx_pct"] = compute_ctx_pct(last_assistant_usage, ctx_window)
