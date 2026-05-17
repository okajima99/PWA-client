"""Anthropic API のレスポンス (= ヘッダ + ResultMessage) を解析して、
state.shared_status / state.agent_status の使用率系フィールドを更新する層。

state.py から責務分離した (2026-05-17): state.py は純粋な state 定義・lifecycle に
専念し、 「使用率の計算」 や「ヘッダの key 名/形式の知識」 は usage.py に集約する。
"""
from datetime import datetime

from state import agent_status, shared_status


def update_shared_from_headers(headers) -> None:
    """Anthropic API のレスポンスヘッダから rate-limit を吸い出して shared_status へ。

    ヘッダ名は単数形 (= `anthropic-ratelimit-unified-5h-reset` / `-7d-reset`)。
    旧コードは「-resets-at」 と複数形で書いていて両方取れず、 5h だけ偶然 SDK の
    RateLimitEvent 経由で値が入っていた (2026-05-17 修正)。
    """
    five_h = headers.get("anthropic-ratelimit-unified-5h-utilization")
    seven_d = headers.get("anthropic-ratelimit-unified-7d-utilization")
    five_h_reset = headers.get("anthropic-ratelimit-unified-5h-reset")
    seven_d_reset = headers.get("anthropic-ratelimit-unified-7d-reset")

    if five_h is not None:
        try:
            shared_status["five_hour_pct"] = round(float(five_h) * 100)
        except ValueError:
            pass
    if seven_d is not None:
        try:
            shared_status["seven_day_pct"] = round(float(seven_d) * 100)
        except ValueError:
            pass
    # reset 値はサーバから unix epoch 数値文字列 (例 "1779015600") で来る。
    # 念のため ISO 8601 文字列形式も両対応。
    if (v := _parse_reset(five_h_reset)) is not None:
        shared_status["five_hour_resets_at"] = v
    if (v := _parse_reset(seven_d_reset)) is not None:
        shared_status["seven_day_resets_at"] = v


def _parse_reset(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def compute_ctx_pct(usage: dict, ctx_window: int = 1_000_000) -> int:
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
    ctx_window = (
        model_usage[model_key].get("contextWindow")
        or agent_status[session_id].get("ctx_window")
        or 1_000_000
    )
    agent_status[session_id]["ctx_window"] = ctx_window
    if last_assistant_usage:
        agent_status[session_id]["ctx_pct"] = compute_ctx_pct(last_assistant_usage, ctx_window)
