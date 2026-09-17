"""钉钉发送辅助测试：加签算法、关键词注入、URL 拼接（不触网）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

import pytest

from app.dingtalk import DingtalkError, apply_keyword, build_url, compute_sign, send_text

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=TOKEN"
SECRET = "SECtest123"


def _expected_sign(secret: str, ts: int) -> str:
    """按官方算法独立复算，用于交叉验证实现。"""
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def test_sign_matches_official_algorithm():
    ts = 1700000000000
    assert compute_sign(SECRET, ts) == _expected_sign(SECRET, ts)


def test_build_url_without_secret_is_untouched():
    """未配置加签时不能改动原 URL（IP 白名单/关键词模式）。"""
    assert build_url(WEBHOOK) == WEBHOOK


def test_build_url_appends_timestamp_and_sign():
    ts = 1700000000000
    url = build_url(WEBHOOK, SECRET, timestamp_ms=ts)
    assert "timestamp=1700000000000" in url
    assert f"sign={_expected_sign(SECRET, ts)}" in url
    # 原有 access_token 必须保留
    assert "access_token=TOKEN" in url


def test_build_url_handles_webhook_without_query_string():
    url = build_url("https://example.com/robot", "s", timestamp_ms=1)
    assert url.startswith("https://example.com/robot?")


def test_build_url_sign_changes_with_timestamp():
    a = build_url(WEBHOOK, SECRET, timestamp_ms=1000)
    b = build_url(WEBHOOK, SECRET, timestamp_ms=2000)
    assert a != b


# ---- 关键词 ----

def test_keyword_is_prepended_when_absent():
    assert apply_keyword("磁盘要满了", "告警") == "告警 磁盘要满了"


def test_keyword_not_duplicated_when_present():
    assert apply_keyword("告警 磁盘要满了", "告警") == "告警 磁盘要满了"


def test_no_keyword_leaves_content_unchanged():
    assert apply_keyword("磁盘要满了", None) == "磁盘要满了"
    assert apply_keyword("磁盘要满了", "") == "磁盘要满了"


# ---- send_text 参数校验（不触网）----

def test_send_text_rejects_empty_webhook():
    with pytest.raises(DingtalkError):
        send_text("", "hello")


def test_send_text_raises_on_unreachable_host():
    with pytest.raises(DingtalkError):
        send_text("https://127.0.0.1:1/robot/send", "hello", timeout=2.0)
