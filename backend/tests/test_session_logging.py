"""session_logging.py の pure 関数 _path_for (= session_id → ログファイルパス、
path injection 防御保険) の unit test。"""
from session_logging import LOG_ROOT, _path_for


def test_path_for_normal_id():
    # 意図: 通常の session_id (= ses_xxxx) はそのまま <root>/<id>.log
    assert _path_for("ses_abc123") == LOG_ROOT / "ses_abc123.log"


def test_path_for_strips_forward_slash():
    # 意図: "/" を含む id は "_" に置換、 LOG_ROOT から抜けない (= 保険)
    p = _path_for("evil/../etc")
    assert p.parent == LOG_ROOT
    # ファイル名部分に "/" が残らないこと (= 結果として "evil_.._etc.log")
    assert "/" not in p.name


def test_path_for_strips_backslash():
    # 意図: "\\" も "_" に置換 (Windows-style path 防御)
    assert _path_for("a\\b") == LOG_ROOT / "a_b.log"
