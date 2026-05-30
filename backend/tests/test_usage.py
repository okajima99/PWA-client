"""usage.py の pure 関数 (= compute_ctx_pct / format_model_name) の
unit test。 すべて side-effect なしで、 fixture も不要。
"""
import json

import usage
from usage import compute_ctx_pct, format_model_name, read_latest_rate_limits


# ============================================================================
# compute_ctx_pct
# ============================================================================

def test_compute_ctx_pct_happy():
    # 意図: input + cache_read + cache_creation の合算で % が出る
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 500,
    }
    assert compute_ctx_pct(usage, ctx_window=10_000) == 35


def test_compute_ctx_pct_caps_at_100():
    # 意図: 合算が window 超過しても 100 で head を打つ (UI 表示 sanity)
    assert compute_ctx_pct({"input_tokens": 20_000}, ctx_window=10_000) == 100


def test_compute_ctx_pct_empty_usage():
    # 意図: usage 辞書空なら 0、 cache_creation 等のキー欠落も 0 扱い
    assert compute_ctx_pct({}, ctx_window=10_000) == 0


def test_compute_ctx_pct_zero_window():
    # 意図: ctx_window <= 0 で ZeroDivision を起こさない
    assert compute_ctx_pct({"input_tokens": 100}, ctx_window=0) == 0


# ============================================================================
# format_model_name
# ============================================================================

def test_format_model_name_opus_4_5():
    # 意図: "claude-opus-4-5-20260101" → "Opus 4.5.20260101" (= UI 表示形式)
    assert format_model_name("claude-opus-4-5-20260101") == "Opus 4.5.20260101"


def test_format_model_name_sonnet():
    # 意図: model family が opus 以外でも capitalize で動く
    assert format_model_name("claude-sonnet-4-7-20260201") == "Sonnet 4.7.20260201"


def test_format_model_name_short_fallback():
    # 意図: parts < 3 のキーは capitalize だけして返す (= ガード)
    assert format_model_name("claude-haiku") == "Haiku"


def test_format_model_name_no_claude_prefix():
    # 意図: prefix 無しでも壊れない (= 入力 sanitize していない側のフォルト保険)
    assert format_model_name("opus-4-5-x") == "Opus 4.5.x"


# ============================================================================
# read_latest_rate_limits (7d% flap 吸収)
# ============================================================================

def _write_rate_limits(tmp_path, rows):
    p = tmp_path / "rate-limits.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def test_seven_day_pct_takes_max_within_same_window(tmp_path, monkeypatch):
    # 同じ reset window 内の 85%↔1% flap は max(=85) に寄せる。
    path = _write_rate_limits(tmp_path, [
        {"seven_day_pct": 85, "seven_day_resets_at": 1000, "five_hour_pct": 30},
        {"seven_day_pct": 1, "seven_day_resets_at": 1000, "five_hour_pct": 31},
    ])
    monkeypatch.setattr(usage, "RATE_LIMITS_LOG_PATH", path)
    out = read_latest_rate_limits()
    assert out["seven_day_pct"] == 85
    assert out["five_hour_pct"] == 31  # 5h は最終行の生値


def test_seven_day_pct_not_masked_across_reset(tmp_path, monkeypatch):
    # window リセット (resets_at が変化) を跨いだら、 リセット直後の低い値を max で隠さない。
    path = _write_rate_limits(tmp_path, [
        {"seven_day_pct": 85, "seven_day_resets_at": 1000},
        {"seven_day_pct": 2, "seven_day_resets_at": 2000},
    ])
    monkeypatch.setattr(usage, "RATE_LIMITS_LOG_PATH", path)
    out = read_latest_rate_limits()
    assert out["seven_day_pct"] == 2


def test_model_ctx_filtered_by_session(tmp_path, monkeypatch):
    # model / ctx は指定 session の最新行から取る (= タブごとの statusline)。
    # 5h/7d は最新行 (= アカウント全体) のまま。
    path = _write_rate_limits(tmp_path, [
        {"session_id": "sidA", "model": "Opus 4.8", "context_pct": 40, "five_hour_pct": 10},
        {"session_id": "sidB", "model": "Haiku 4.5", "context_pct": 5, "five_hour_pct": 11},
    ])
    monkeypatch.setattr(usage, "RATE_LIMITS_LOG_PATH", path)
    out_a = read_latest_rate_limits("sidA")
    assert out_a["model"] == "Opus 4.8" and out_a["context_pct"] == 40
    assert out_a["five_hour_pct"] == 11  # 5h はアカウント全体 = 最新行
    out_b = read_latest_rate_limits("sidB")
    assert out_b["model"] == "Haiku 4.5" and out_b["context_pct"] == 5


def test_model_ctx_none_when_session_absent(tmp_path, monkeypatch):
    # 指定 session の行が tail に無ければ model/ctx は None (= 呼び出し側が agent_status に fallback)。
    path = _write_rate_limits(tmp_path, [
        {"session_id": "sidA", "model": "Opus 4.8", "context_pct": 40, "five_hour_pct": 10},
    ])
    monkeypatch.setattr(usage, "RATE_LIMITS_LOG_PATH", path)
    out = read_latest_rate_limits("sidX")
    assert out["model"] is None and out["context_pct"] is None
    assert out["five_hour_pct"] == 10  # 5h は取れる
