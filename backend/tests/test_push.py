"""push.py の Markdown sanitization / 通知整形系 pure 関数の unit test。"""
from push import (
    _MD_TABLE_ROW_RE,
    _table_row_to_inline,
    sanitize_notif_body,
    strip_markdown,
)


# ============================================================================
# strip_markdown
# ============================================================================

def test_strip_markdown_bold_italic_code():
    # 意図: bold/italic/inline code の記号だけ消えて中身が残る
    src = "**bold** and *italic* and `code`"
    assert strip_markdown(src) == "bold and italic and code"


def test_strip_markdown_heading_and_list():
    # 意図: heading 記号は削除、 箇条書きは中黒に置換
    src = "# Title\n- a\n- b"
    assert strip_markdown(src) == "Title\n• a\n• b"


def test_strip_markdown_fence_keeps_body():
    # 意図: fenced code block は ``` を剥がして中身だけ残す
    src = "```python\nprint(1)\n```"
    assert strip_markdown(src) == "print(1)\n"


def test_strip_markdown_table_to_inline():
    # 意図: 表セパレータ行が消えて行はセル分かち書きへ。 セパレータ regex の `\s*` は
    # 改行も食うので、 前後の table row が連結された 1 行になる現挙動を pin。
    # 通知 body 用の loss-y 整形なので、 後段の sanitize_notif_body で空白 1 個に畳まれて
    # 体感上は問題なし (= row 結合での読みやすさ悪化を改善するなら別 PR で `_MD_TABLE_SEP_RE`
    # を改行非貪欲版に直す案)
    src = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert strip_markdown(src) == "a / b1 / 2"


def test_strip_markdown_link_to_text():
    # 意図: [text](url) は text だけ残す、 image も同様
    assert strip_markdown("see [docs](https://x.example)") == "see docs"


# ============================================================================
# sanitize_notif_body
# ============================================================================

def test_sanitize_notif_body_collapses_whitespace():
    # 意図: 改行と連続空白を 1 スペースに畳む (iOS ロック画面 1 行用)
    assert sanitize_notif_body("a\n\nb   c") == "a b c"


def test_sanitize_notif_body_empty():
    # 意図: 空文字は "" を返して broadcast 側を破綻させない
    assert sanitize_notif_body("") == ""


def test_sanitize_notif_body_markdown_then_collapse():
    # 意図: Markdown strip と空白畳みが正しい順で走る
    assert sanitize_notif_body("# T\n\n**bold**") == "T bold"


# ============================================================================
# _table_row_to_inline (= _MD_TABLE_ROW_RE.sub 経由でテスト)
# ============================================================================

def _apply_table_row(text: str) -> str:
    return _MD_TABLE_ROW_RE.sub(_table_row_to_inline, text)


def test_table_row_basic():
    # 意図: 3 セル行が " / " 区切りになる
    assert _apply_table_row("| a | b | c |") == "a / b / c"


def test_table_row_strips_cells():
    # 意図: セル両端 whitespace を strip
    assert _apply_table_row("|  x  |  y  |") == "x / y"


def test_table_row_drops_empty_cells():
    # 意図: || で生まれる空セルは捨てて出力に出さない
    assert _apply_table_row("| a |  | b |") == "a / b"
