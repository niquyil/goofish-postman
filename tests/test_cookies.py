"""cookie 工具测试：字符串 ↔ jar 互转，以及按名字删 cookie。"""

from __future__ import annotations

from goofishpostman.cookies import Cookies, drop_cookies


def test_from_str_keeps_values_with_equals_signs() -> None:
    """cookie 值里可能有 `=`（base64 之类），不能把后面的部分截断。"""
    cookies = Cookies.from_str('unb=1; sgcookie=E100abc%2Fd==; tracknick=名字; empty=')

    assert cookies.get('sgcookie') == 'E100abc%2Fd=='
    assert cookies.get('tracknick') == '名字'
    assert cookies.get('empty') == ''
    assert str(cookies).startswith('unb=1; ')


def test_drop_cookies_removes_only_the_named_ones() -> None:
    """RequestsCookieJar.clear 必须带 domain/path，这里替调用方补上（漏了就删不掉）。"""
    jar = Cookies.from_str('unb=1; _m_h5_tk=abc_1; _m_h5_tk_enc=enc; tracknick=x')

    dropped = drop_cookies(jar, '_m_h5_tk', '_m_h5_tk_enc')

    assert sorted(dropped) == ['_m_h5_tk', '_m_h5_tk_enc']
    assert jar.get('_m_h5_tk') is None
    assert jar.get('_m_h5_tk_enc') is None
    assert jar.get('unb') == '1'
    assert drop_cookies(jar, '不存在的') == []
