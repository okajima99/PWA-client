"""usage.py の pure 関数 (= compute_ctx_pct / format_model_name) の
unit test。 すべて side-effect なしで、 fixture も不要。
"""
from usage import compute_ctx_pct, format_model_name


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
