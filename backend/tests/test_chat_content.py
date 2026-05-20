"""chat_content.py の build_content (= Anthropic content 配列組立) の unit test。
ファイル I/O は tmp_path で隔離、 副作用は test 内に閉じる。
"""
from chat_content import build_content


def test_build_content_message_only():
    # 意図: 添付なしなら text 1 個の content 配列
    assert build_content("hello", []) == [{"type": "text", "text": "hello"}]


def test_build_content_image_attached(tmp_path):
    # 意図: image MIME は base64 image content + パス text、 そのあと message text
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n")
    saved = [{"name": "x.png", "path": str(p), "mime": "image/png"}]

    out = build_content("msg", saved)

    assert out[0]["type"] == "image"
    assert out[0]["source"]["media_type"] == "image/png"
    assert out[0]["source"]["type"] == "base64"
    assert out[1] == {"type": "text", "text": f"[添付画像のパス: {p}]"}
    assert out[-1] == {"type": "text", "text": "msg"}


def test_build_content_text_file_fenced(tmp_path):
    # 意図: 非画像はテキスト読み込み + fenced code として 1 個の text content
    p = tmp_path / "a.txt"
    p.write_text("body line")
    saved = [{"name": "a.txt", "path": str(p), "mime": "text/plain"}]

    out = build_content("", saved)

    assert len(out) == 1
    assert out[0]["type"] == "text"
    assert "body line" in out[0]["text"]
    assert out[0]["text"].startswith("[添付ファイル: ")
    assert "```" in out[0]["text"]


def test_build_content_empty_message_with_image_only(tmp_path):
    # 意図: message 空 + 画像のみでも image + パス text が残るので fallback は走らない
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    saved = [{"name": "x.png", "path": str(p), "mime": "image/png"}]

    out = build_content("", saved)

    assert any(c.get("type") == "image" for c in out)
    # fallback の "[添付ファイル N 件: ..]" 形式は出ない
    assert not any(c.get("text", "").startswith("[添付ファイル ") for c in out)


def test_build_content_fallback_when_reads_fail(tmp_path):
    # 意図: 不在ファイル + message 空 = read 全失敗 → 「N 件添付」 text を fallback
    saved = [{
        "name": "missing.txt",
        "path": str(tmp_path / "missing.txt"),
        "mime": "text/plain",
    }]

    out = build_content("", saved)

    assert out == [{"type": "text", "text": "[添付ファイル 1 件: missing.txt]"}]
