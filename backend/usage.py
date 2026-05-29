"""使用率系の状態 (= 5h/7d/ctx/model) を組み立てる層。

rate-limits.jsonl (= statusline 記録) の読み取りと、 usage からの context 使用率計算を
担当する。 state.py は純粋な state 定義・lifecycle に専念し、 「使用率の計算」 はここに
集約する (= 2026-05-17 責務分離)。
"""
import json

from config import RATE_LIMITS_LOG_PATH
from state import DEFAULT_CTX_WINDOW


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
    parsed: list[dict] = []
    for ln in lines[-8:]:
        try:
            parsed.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            continue
    if not parsed:
        return {}
    last = parsed[-1]
    # 7d% flap 吸収: Anthropic 側集計が 85%↔1% で一時的に揺らぐ (= 2026-05-29 早朝に観測)。
    # 同じ seven_day_resets_at を共有する直近行の中で max を採り、 単調側に寄せて瞬間的な
    # 下振れを潰す。 resets_at が変わった行 (= 正常な window リセット) は別 window なので
    # 対象外にして、 リセット直後の正当な下振れまで max で隠さない。
    cur_reset = last.get("seven_day_resets_at")
    seven_day_pct = last.get("seven_day_pct")
    same_window = [
        p.get("seven_day_pct") for p in parsed
        if p.get("seven_day_resets_at") == cur_reset
        and isinstance(p.get("seven_day_pct"), (int, float))
    ]
    if same_window:
        seven_day_pct = max(same_window)
    return {
        "five_hour_pct": last.get("five_hour_pct"),
        "seven_day_pct": seven_day_pct,
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


