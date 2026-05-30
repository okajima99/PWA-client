"""prompt_watch.extract_prompt の unit test。

実機 capture-pane で観測したプロンプト形式 (= session survey の "1: Bad ..." 横並び、
plan の "1. Yes" 縦並び) を質問 + 選択肢に分解できること、 稼働中マーカーが出てる時は
None を返すことを担保する。
"""
from prompt_watch import extract_prompt


# 実機 (= e37100343cd9 タブ) で観測した session survey の末尾 (= idle 状態。 稼働中なら
# Running… 等が出るが、 待ちに入った時はそれが消える)
SURVEY_PANE = """\
⏺ 了解しました。
✻ Worked for 8s
● How is Claude doing this session? (optional)
  1: Bad    2: Fine   3: Good   0: Dismiss
────────────────────────────────────────────
❯
────────────────────────────────────────────
  [Opus 4.8] 5h:28% 7d:22% ctx:44%
"""

PLAN_PANE = """\
Here is the plan to proceed.
Do you want to proceed?
1. Yes
2. No, keep planning
────────────────────────────────────────────
❯
"""

WORKING_PANE = """\
✻ Sublimating… (2m 34s · ↓ 3.1k tokens)
  1: Bad    2: Fine   3: Good   0: Dismiss
❯
"""


def test_survey_is_filtered_as_noise():
    # セッション品質 survey は操作上ノイズなので拾わない (= None)。
    assert extract_prompt(SURVEY_PANE) is None


def test_extract_plan_multiline_options():
    out = extract_prompt(PLAN_PANE)
    assert out is not None
    keys = [o["key"] for o in out["options"]]
    assert keys == ["1", "2"]
    assert "Do you want to proceed?" in out["question"]


def test_working_marker_returns_none():
    # 稼働中マーカー (… (Ns · ↓ tokens) があれば、 選択肢が見えてても生成中なので無視。
    assert extract_prompt(WORKING_PANE) is None


def test_no_options_returns_none():
    assert extract_prompt("ただの出力\nなにもプロンプトは無い\n❯\n") is None


def test_single_option_returns_none():
    # 選択肢が 1 個だけ (= 通常の番号付き文章) は誤検出しない。
    assert extract_prompt("1. これは普通の箇条書き\n❯\n") is None
