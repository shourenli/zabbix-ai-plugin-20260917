"""钉钉群机器人发送：支持"自定义关键词"与"加签"两种安全设置。

- 关键词模式：消息内容必须包含关键词，否则钉钉返回 errcode 310000
- 加签模式：需在 URL 追加 timestamp 与 sign（HMAC-SHA256）
- IP 白名单：服务端出口 IP 加白即可，无需改代码

官方文档：https://open.dingtalk.com/document/orgapp/custom-robot-access
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Optional

import httpx


class DingtalkError(Exception):
    """钉钉发送失败（网络异常或 errcode != 0）。"""


def compute_sign(secret: str, timestamp_ms: int) -> str:
    """按官方算法计算加签值，返回 URL 编码后的 sign。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def build_url(
    webhook: str, secret: Optional[str] = None, timestamp_ms: Optional[int] = None
) -> str:
    """拼接最终请求 URL；配置了 secret 时追加 timestamp 与 sign。"""
    if not secret:
        return webhook
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={compute_sign(secret, ts)}"


def apply_keyword(content: str, keyword: Optional[str]) -> str:
    """关键词模式下确保内容包含关键词；已包含时不重复添加。"""
    if not keyword:
        return content
    if keyword in content:
        return content
    return f"{keyword} {content}"


def send_text(
    webhook: str,
    content: str,
    *,
    secret: Optional[str] = None,
    keyword: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """发送文本消息，返回钉钉响应体；失败抛 DingtalkError。"""
    if not webhook:
        raise DingtalkError("供应商 webhook 未配置")

    url = build_url(webhook, secret)
    payload = {
        "msgtype": "text",
        "text": {"content": apply_keyword(content, keyword)},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        body = resp.json()
    except Exception as exc:  # 网络异常 / 非 JSON 响应
        raise DingtalkError(f"钉钉请求失败: {exc}") from exc

    if body.get("errcode", -1) != 0:
        raise DingtalkError(
            f"钉钉返回错误 errcode={body.get('errcode')} errmsg={body.get('errmsg')}"
        )
    return body
