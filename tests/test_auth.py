"""鉴权测试：zbx_session cookie 解析与用户会话校验（对应验收点"直访被拒"）。"""
from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

from app.auth import SessionError, authenticate, parse_session_cookie
from app.zabbix import Unauthorized


def _encode(payload) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


class FakeZabbixClient:
    """可控的假客户端，避免测试触网。"""

    def __init__(self, user=None, raise_unauthorized: bool = False):
        self._user = user
        self._raise = raise_unauthorized

    def user_get(self, sessionid):
        if self._raise:
            raise Unauthorized(-32602, "Session terminated, re-login, please.")
        return self._user


# ---- parse_session_cookie ----

def test_parses_valid_base64_json_cookie():
    assert parse_session_cookie(_encode({"sessionid": "abc123", "sign": "x"})) == "abc123"


def test_parses_url_encoded_cookie_from_real_browser():
    """真实浏览器发送的 zbx_session 被 PHP setcookie() URL 编码过（= 变成 %3D）。

    回归测试：此前未做 URL 解码，导致 base64 解析失败、有效会话被误判为失效，
    dashboard 里表现为"会话已过期，请先登录 Zabbix"，且重新登录也无效。
    """
    # 用真实长度的 sessionid（32 位十六进制），其 base64 会带 "==" 填充
    real_sid = "a" * 32
    raw = _encode({"sessionid": real_sid, "sign": "x"})
    encoded = urllib.parse.quote(raw, safe="")
    assert "%3D" in encoded  # 确认构造出了真实场景的编码形式（结尾 = 被编码）
    assert parse_session_cookie(encoded) == real_sid


def test_url_encoded_cookie_without_sessionid_returns_none():
    encoded = urllib.parse.quote(_encode({"sign": "only-sign"}), safe="")
    assert parse_session_cookie(encoded) is None


def test_plain_token_with_percent_escapes_is_decoded():
    """非 JSON 的纯 token 也应按 URL 解码后的值使用。"""
    assert parse_session_cookie("tok%2Ben%2Fslash%3D") == "tok+en/slash="


def test_missing_or_empty_cookie_returns_none():
    assert parse_session_cookie(None) is None
    assert parse_session_cookie("") is None


def test_plain_text_token_falls_back_to_raw_value():
    assert parse_session_cookie("raw-token-value") == "raw-token-value"


def test_valid_base64_but_not_json_falls_back_to_raw():
    value = base64.b64encode(b"not-json-at-all").decode("ascii")
    assert parse_session_cookie(value) == value


def test_json_without_sessionid_returns_none():
    value = _encode({"sign": "only-sign"})
    assert parse_session_cookie(value) is None


# ---- authenticate ----

def test_authenticate_without_cookie_raises_session_error():
    with pytest.raises(SessionError):
        authenticate({}, FakeZabbixClient(), "user")


def test_authenticate_with_invalid_session_raises_session_error():
    cookies = {"zbx_session": _encode({"sessionid": "expired"})}
    with pytest.raises(SessionError):
        authenticate(cookies, FakeZabbixClient(raise_unauthorized=True), "user")


def test_authenticate_with_empty_user_list_raises_session_error():
    cookies = {"zbx_session": _encode({"sessionid": "s"})}
    with pytest.raises(SessionError):
        authenticate(cookies, FakeZabbixClient(user=[]), "user")


def test_authenticate_success_returns_user_context():
    cookies = {"zbx_session": _encode({"sessionid": "sess-1"})}
    client = FakeZabbixClient(
        user=[{"userid": "7", "username": "alice", "roleid": "3"}]
    )
    ctx = authenticate(cookies, client, "user")
    assert ctx["userid"] == "7"
    assert ctx["username"] == "alice"
    assert ctx["roleid"] == "3"
    assert ctx["sessionid"] == "sess-1"


def test_authenticate_accepts_single_object_user_response():
    """部分版本 user.get 返回对象而非列表。"""
    cookies = {"zbx_session": _encode({"sessionid": "sess-2"})}
    client = FakeZabbixClient(user={"userid": "9", "username": "bob", "roleid": "1"})
    ctx = authenticate(cookies, client, "user")
    assert ctx["username"] == "bob"
    assert ctx["sessionid"] == "sess-2"


def test_authenticate_service_mode_still_requires_login():
    with pytest.raises(SessionError):
        authenticate({}, FakeZabbixClient(), "service")
