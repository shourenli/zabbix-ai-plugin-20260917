"""访问控制：校验 zbx_session cookie。

zabbix 前端在 6.0/7.0 均使用名为 zbx_session 的 cookie，内容为 base64 JSON：
    {"sessionid": "...", "sign": "..."}

⚠️ 关键：Zabbix 用 PHP 的 setcookie() 下发该 cookie，值会被 **URL 编码**
（base64 结尾的 '=' 变成 %3D），浏览器原样回传。PHP 读取 $_COOKIE 时会自动解码，
因此这里也必须先 URL 解码，否则 base64 解析失败，有效会话会被误判为失效（401）。

异常语义（调用方据此返回不同状态码）：
- SessionError    会话缺失/失效 → 401，提示重新登录
- AuthUnavailable 无法校验（zabbix API 不可达）→ 503，不是用户身份问题
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from typing import Any, Dict, Optional

from .zabbix import Unauthorized, ZabbixClient, ZabbixError

logger = logging.getLogger("zabbix_ai.auth")

SESSION_COOKIE = "zbx_session"

# 表示"该候选值不是 base64 JSON"，需要继续尝试其它形式
_NOT_JSON = object()


class SessionError(Exception):
    """会话缺失或失效，需要登录。"""


class AuthUnavailable(Exception):
    """无法完成会话校验（zabbix API 不可达）。"""


def _sessionid_from(candidate: str):
    """解析 base64(JSON)；返回 sessionid / None（是 JSON 但无 sessionid）/ _NOT_JSON。"""
    try:
        data = json.loads(base64.b64decode(candidate))
    except Exception:
        return _NOT_JSON
    if not isinstance(data, dict):
        return _NOT_JSON
    sid = data.get("sessionid")
    return sid if sid else None


def parse_session_cookie(value: Optional[str]) -> Optional[str]:
    """从 cookie 值解析 sessionid；失败返回 None。

    先按 URL 解码后的值解析（真实浏览器场景），再尝试原始值，
    最后兼容"整个值就是纯 token"的部署形式。
    """
    if not value:
        return None

    decoded = urllib.parse.unquote(value)
    for candidate in (decoded, value):
        result = _sessionid_from(candidate)
        if result is _NOT_JSON:
            continue
        return result  # 解析成功：sessionid 字符串，或"是 JSON 但缺 sessionid"→None

    # 都不是 base64 JSON：按纯字符串 token 处理
    return decoded or None


def _lookup_user(client: ZabbixClient, sessionid: str) -> Dict[str, Any]:
    """用会话 token 取用户；区分"会话失效"与"zabbix 不可达"。"""
    try:
        user = client.user_get(sessionid)
    except Unauthorized as exc:
        logger.warning(
            "会话校验失败：zabbix 判定 sessionid 无效（前8位 %s）", sessionid[:8]
        )
        raise SessionError("invalid session") from exc
    except ZabbixError as exc:
        raise AuthUnavailable(f"zabbix 暂不可用: {exc}") from exc

    if not user:
        logger.warning("会话校验失败：user.get 返回空（会话可能已过期）")
        raise SessionError("invalid session")
    first = user[0] if isinstance(user, list) else user
    if not first:
        logger.warning("会话校验失败：user.get 返回空对象")
        raise SessionError("invalid session")
    return first


def authenticate(
    request_cookies: Dict[str, str], client: ZabbixClient, auth_mode: str
) -> Dict[str, Any]:
    """校验请求会话，返回 {userid, username, roleid, sessionid}。

    auth_mode 目前对校验流程一致（都要求用户已登录 zabbix）；
    service 模式仅影响数据查询使用哪个账号（见 zabbix.ZabbixClient）。
    """
    raw = request_cookies.get(SESSION_COOKIE)
    sid = parse_session_cookie(raw)
    if not sid:
        logger.info(
            "鉴权失败：未携带有效的 %s cookie（%s）",
            SESSION_COOKIE,
            "cookie 缺失" if not raw else "cookie 无法解析出 sessionid",
        )
        raise SessionError("missing session")

    first = _lookup_user(client, sid)
    return {
        "userid": first.get("userid"),
        "username": first.get("username"),
        "roleid": first.get("roleid"),
        "sessionid": sid,
    }
