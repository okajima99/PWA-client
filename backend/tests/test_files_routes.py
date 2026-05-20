"""files_routes.py の _resolve_safe (= path injection 防御) の unit test。"""
import pytest
from fastapi import HTTPException

from config import HOME
from files_routes import _resolve_safe


def test_resolve_safe_inside_home():
    # 意図: HOME 配下のパスは resolve されてそのまま返る
    p = _resolve_safe(str(HOME / "x" / "y"))
    assert str(p).startswith(str(HOME))


def test_resolve_safe_tilde_expansion():
    # 意図: "~/foo" は HOME 配下に展開される
    p = _resolve_safe("~/foo.txt")
    assert p == HOME / "foo.txt"


def test_resolve_safe_outside_home_raises():
    # 意図: /etc/passwd 等 HOME 外は 403 (path injection 防御)
    with pytest.raises(HTTPException) as exc_info:
        _resolve_safe("/etc/passwd")
    assert exc_info.value.status_code == 403


def test_resolve_safe_dotdot_escape_raises():
    # 意図: HOME 配下から .. で抜けようとしても resolve 後の prefix 判定で止まる
    with pytest.raises(HTTPException) as exc_info:
        _resolve_safe(str(HOME) + "/../../etc")
    assert exc_info.value.status_code == 403
