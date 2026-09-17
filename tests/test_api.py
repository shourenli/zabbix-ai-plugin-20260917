"""接口层测试：未登录访问必须被拒（核心安全验收点），以及健康检查与静态页。

所有测试均通过 monkeypatch 隔断网络，不依赖真实 zabbix / MySQL。
"""
from __future__ import annotations

import base64
import json
import urllib.parse

from fastapi.testclient import TestClient

from app.main import app
from app.zabbix import Unauthorized, ZabbixError

client = TestClient(app)


def _cookie(sessionid: str = "sess-ok") -> str:
    return base64.b64encode(
        json.dumps({"sessionid": sessionid, "sign": "x"}).encode("utf-8")
    ).decode("ascii")


def _user(userid: str = "1", username: str = "alice", roleid: str = "3"):
    return [{"userid": userid, "username": username, "roleid": roleid}]


def _raise_session_invalid(sessionid):
    raise Unauthorized(-32602, "Session terminated, re-login, please.")


def _raise_zabbix_down(sessionid):
    raise ZabbixError(0, "Zabbix 连接失败: getaddrinfo failed")


def _return_no_user(sessionid):
    return []


# ---- 未登录 / 伪造会话：一律 401 ----

def test_chat_without_session_is_rejected():
    r = client.post("/api/chat", json={"message": "最近有哪些告警"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_history_without_session_is_rejected():
    assert client.get("/api/history").status_code == 401


def test_audit_without_session_is_rejected():
    assert client.get("/api/audit").status_code == 401


def test_clear_flag_also_requires_session():
    r = client.post("/api/chat", json={"message": "", "clear": True})
    assert r.status_code == 401


def test_forged_cookie_is_rejected(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", _raise_session_invalid)
    r = client.post(
        "/api/chat",
        json={"message": "hi"},
        cookies={"zbx_session": "forged-session-id"},
    )
    assert r.status_code == 401


def test_cookie_pointing_to_no_user_is_rejected(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", _return_no_user)
    r = client.post(
        "/api/chat",
        json={"message": "hi"},
        cookies={"zbx_session": "unknown-session"},
    )
    assert r.status_code == 401


# ---- zabbix 不可达：503（不是 401）----

def test_zabbix_unreachable_returns_503_not_401(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", _raise_zabbix_down)
    r = client.post(
        "/api/chat",
        json={"message": "hi"},
        cookies={"zbx_session": "any-session"},
    )
    assert r.status_code == 503
    assert r.json()["error"] == "zabbix_unavailable"


# ---- 静态页与健康检查 ----

def test_index_page_is_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "Zabbix AI" in r.text


def test_index_is_not_cached():
    """升级插件后用户刷新即应拿到新前端，避免看到旧页面。"""
    r = client.get("/")
    assert "no-store" in r.headers.get("cache-control", "")


def test_index_supports_theme_following():
    """前端需支持亮/暗主题，避免与 Zabbix 配色冲突。"""
    text = client.get("/").text
    assert 'data-theme="light"' in text
    assert 'data-theme="dark"' in text
    assert "prefers-color-scheme" in text


def test_health_endpoint_reports_components():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["mysql"], bool)
    assert body["llm_model"]
    assert body["zabbix_api_configured"] is True


def test_health_does_not_leak_supplier_names():
    """健康检查未鉴权即可访问，不应暴露供应商名单。"""
    assert "suppliers" not in client.get("/api/health").json()


# ---- 已登录用户：供应商提示与审计权限 ----

def test_history_returns_suppliers_for_authenticated_user(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user())
    monkeypatch.setattr(
        "app.main.storage.get_context",
        lambda key, limit=50, ttl_seconds=0: {"messages": [], "expired": False},
    )
    r = client.get("/api/history", cookies={"zbx_session": _cookie()})
    assert r.status_code == 200
    body = r.json()
    assert body["messages"] == []
    assert isinstance(body["suppliers"], list)
    assert body["suppliers"], "应返回已注册供应商，供前端 / 命令提示"


def test_audit_forbidden_for_non_admin(monkeypatch):
    monkeypatch.setattr(
        "app.main.zclient.user_get", lambda sid: _user("2", "bob", roleid="1")
    )
    r = client.get("/api/audit", cookies={"zbx_session": _cookie()})
    assert r.status_code == 403


def test_audit_allowed_for_super_admin(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user())
    monkeypatch.setattr(
        "app.main.storage.recent_audit",
        lambda username=None, limit=50: [
            {
                "username": "alice",
                "action": "send_dingtalk",
                "target": "网络供应商A",
                "detail": "OK: 出口丢包",
                "ts": 1,
            }
        ],
    )
    r = client.get("/api/audit", cookies={"zbx_session": _cookie()})
    assert r.status_code == 200
    assert r.json()["logs"][0]["action"] == "send_dingtalk"


# ---- 斜杠命令：确定性执行（不得交给 LLM 决策，否则会出现"未发送却报成功"）----

def _auth_and_silence_storage(monkeypatch, roleid: str = "3"):
    monkeypatch.setattr(
        "app.main.zclient.user_get", lambda sid: _user(roleid=roleid)
    )
    monkeypatch.setattr("app.main.storage.add_message", lambda *a, **k: None)
    monkeypatch.setattr("app.main.storage.clear_messages", lambda *a, **k: 0)
    # /api/chat 现在会先取上下文（含闲置超时判定），同样要隔断真实 MySQL
    monkeypatch.setattr(
        "app.main.storage.get_context",
        lambda key, limit=50, ttl_seconds=0: {"messages": [], "expired": False},
    )


def test_slash_command_sends_and_reports_actual_success(monkeypatch):
    _auth_and_silence_storage(monkeypatch)
    sent = {}

    def fake_send(ctx, args):
        sent.update(args)
        return {
            "ok": True,
            "supplier": "网络供应商A",
            "sent_at": "2026-01-01T00:00:00+00:00",
            "message": args["message"],
        }

    monkeypatch.setattr("app.main.tool_send_dingtalk", fake_send)
    r = client.post(
        "/api/chat",
        cookies={"zbx_session": _cookie()},
        json={"message": "/网络供应商A 出口丢包，请排查"},
    )
    assert r.status_code == 200
    assert "已发送到「网络供应商A」" in r.json()["reply"]
    assert sent == {"supplier": "网络供应商A", "message": "出口丢包，请排查"}


def test_slash_command_never_consults_llm(monkeypatch):
    """关键：发送动作必须由代码确定执行，不能依赖模型是否决定调用工具。"""
    _auth_and_silence_storage(monkeypatch)
    monkeypatch.setattr(
        "app.main.tool_send_dingtalk",
        lambda ctx, args: {
            "ok": True,
            "supplier": args["supplier"],
            "sent_at": "t",
            "message": args["message"],
        },
    )

    def boom(*a, **k):
        raise AssertionError("斜杠命令不应经过 LLM 决策")

    monkeypatch.setattr("app.main._call_llm", boom)
    r = client.post(
        "/api/chat", cookies={"zbx_session": _cookie()}, json={"message": "/网络供应商A 测试"}
    )
    assert r.status_code == 200


def test_slash_command_without_message_returns_usage_without_sending(monkeypatch):
    _auth_and_silence_storage(monkeypatch)
    called = []
    monkeypatch.setattr(
        "app.main.tool_send_dingtalk", lambda ctx, args: called.append(args) or {"ok": True}
    )
    r = client.post("/api/chat", cookies={"zbx_session": _cookie()}, json={"message": "/"})
    reply = r.json()["reply"]
    assert "斜杠命令格式" in reply
    assert "网络供应商A" in reply  # 列出可用供应商
    assert called == []


def test_slash_command_reports_dingtalk_error_verbatim(monkeypatch):
    """钉钉报错必须原样呈现，不能包装成成功。"""
    _auth_and_silence_storage(monkeypatch)
    monkeypatch.setattr(
        "app.main.tool_send_dingtalk",
        lambda ctx, args: {
            "error": "钉钉返回错误 errcode=40035 errmsg=缺少参数 access_token"
        },
    )
    r = client.post(
        "/api/chat", cookies={"zbx_session": _cookie()}, json={"message": "/网络供应商A 测试"}
    )
    reply = r.json()["reply"]
    assert "发送失败" in reply
    assert "40035" in reply


def test_unknown_supplier_is_refused(monkeypatch):
    _auth_and_silence_storage(monkeypatch)
    monkeypatch.setattr(
        "app.main.tool_send_dingtalk",
        lambda ctx, args: {"error": f"未注册供应商: {args['supplier']}。可选: 网络供应商A"},
    )
    r = client.post(
        "/api/chat", cookies={"zbx_session": _cookie()}, json={"message": "/野供应商 测试"}
    )
    assert "发送失败" in r.json()["reply"]


def test_chat_accepts_url_encoded_session_cookie(monkeypatch):
    """回归：真实浏览器发送的 zbx_session 是 URL 编码的，必须能通过鉴权。

    用显式 Cookie 头传入，避免测试客户端对 cookie 值再做一次编码。
    """
    _auth_and_silence_storage(monkeypatch)
    monkeypatch.setattr("app.main._call_llm", lambda user, text: "ok")
    encoded = urllib.parse.quote(_cookie(), safe="")
    r = client.post(
        "/api/chat",
        headers={"Cookie": f"zbx_session={encoded}"},
        json={"message": "最近有哪些告警"},
    )
    assert r.status_code == 200
    assert r.json()["reply"] == "ok"


# ---- 对话隔离与闲置清空（v1.0.7）----
# 需求：注销后重登不应再看到上一轮对话；闲置 15 分钟后自动开始新会话。
# 关键事实（已实测）：Zabbix 每次登录都下发新的 sessionid，同一会话内稳定，注销即失效。
# 因此只要用 sessionid 当键，"重登即新会话"就是结构性保证，不依赖前端能否捕获注销。

def _capture_context(monkeypatch, expired=False, messages=None):
    """隔断存储并记录 get_context 收到的键、条数上限与 TTL。"""
    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user("1", "alice"))
    monkeypatch.setattr("app.main.storage.add_message", lambda *a, **k: None)
    monkeypatch.setattr("app.main.llm.chat", lambda *a, **k: "ok")
    seen = {}

    def fake_get_context(key, limit=50, ttl_seconds=0):
        seen["key"] = key
        seen["limit"] = limit
        seen["ttl_seconds"] = ttl_seconds
        return {"messages": list(messages or []), "expired": expired}

    monkeypatch.setattr("app.main.storage.get_context", fake_get_context)
    return seen


def test_conversation_is_keyed_by_login_session_not_userid(monkeypatch):
    seen_a = _capture_context(monkeypatch)
    client.post("/api/chat", json={"message": "hi"},
                cookies={"zbx_session": _cookie("sess-A")})
    assert seen_a["key"] == "sess-A", "必须按登录会话隔离，而不是按 userid"

    # 同一个人、换一次登录 -> 另一个键，天然读不到上一轮
    seen_b = _capture_context(monkeypatch)
    client.post("/api/chat", json={"message": "hi"},
                cookies={"zbx_session": _cookie("sess-B")})
    assert seen_b["key"] == "sess-B"
    assert seen_a["key"] != seen_b["key"]


def test_chat_passes_idle_ttl_to_storage(monkeypatch):
    from app.config import settings

    seen = _capture_context(monkeypatch)
    client.post("/api/chat", json={"message": "hi"}, cookies={"zbx_session": _cookie()})
    assert seen["ttl_seconds"] == settings.history_ttl_minutes * 60
    assert seen["ttl_seconds"] == 15 * 60, "默认应为闲置 15 分钟即新会话"
    assert seen["limit"] == settings.history_max_messages


def test_chat_reports_context_reset_so_ui_can_clear(monkeypatch):
    _capture_context(monkeypatch, expired=True,
                     messages=[{"role": "user", "content": "旧"}])
    r = client.post("/api/chat", json={"message": "hi"}, cookies={"zbx_session": _cookie()})
    assert r.status_code == 200
    assert r.json()["context_reset"] is True


def test_chat_without_reset_reports_false(monkeypatch):
    _capture_context(monkeypatch)
    r = client.post("/api/chat", json={"message": "hi"}, cookies={"zbx_session": _cookie()})
    assert r.json()["context_reset"] is False


def test_history_exposes_ttl_and_reset_flag(monkeypatch):
    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user())
    monkeypatch.setattr(
        "app.main.storage.get_context",
        lambda key, limit=50, ttl_seconds=0: {"messages": [], "expired": True},
    )
    r = client.get("/api/history", cookies={"zbx_session": _cookie()})
    body = r.json()
    assert body["ttl_minutes"] == 15
    assert body["context_reset"] is True


def test_expired_context_is_not_sent_to_the_model(monkeypatch):
    """闲置过期后旧对话不能再进入模型上下文，否则等于没清。"""
    captured = {}

    def fake_chat(history, tools=None, function_handlers=None):
        captured["history"] = history
        return "ok"

    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user())
    monkeypatch.setattr("app.main.storage.add_message", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.main.storage.get_context",
        lambda key, limit=50, ttl_seconds=0: {"messages": [], "expired": True},
    )
    monkeypatch.setattr("app.main.llm.chat", fake_chat)
    client.post("/api/chat", json={"message": "新问题"}, cookies={"zbx_session": _cookie()})
    assert [m["role"] for m in captured["history"]] == ["system", "user"]
    assert captured["history"][-1]["content"] == "新问题"


def test_current_message_appears_once_in_model_context(monkeypatch):
    """回归：旧实现既写库又把本轮消息追加到历史，同一句话在上下文里出现两遍。"""
    monkeypatch.setattr("app.main.zclient.user_get", lambda sid: _user())
    monkeypatch.setattr("app.main.storage.add_message", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.main.storage.get_context",
        lambda key, limit=50, ttl_seconds=0: {
            "messages": [
                {"role": "user", "content": "上一句"},
                {"role": "assistant", "content": "上一答"},
            ],
            "expired": False,
        },
    )
    captured = {}

    def fake_chat(history, tools=None, function_handlers=None):
        captured["history"] = history
        return "ok"

    monkeypatch.setattr("app.main.llm.chat", fake_chat)
    client.post("/api/chat", json={"message": "这一句"}, cookies={"zbx_session": _cookie()})
    contents = [m["content"] for m in captured["history"] if m["role"] == "user"]
    assert contents == ["上一句", "这一句"], "本轮消息只能出现一次，且应排在历史之后"


# ---- 许可证与「关于」入口（GPL-3.0 §5(d) 的界面通知）----

def test_license_route_is_public_and_returns_gpl():
    """许可证全文必须无需登录即可访问（法律声明不应被鉴权挡住）。"""
    r = client.get("/license")
    assert r.status_code == 200
    assert "GNU GENERAL PUBLIC LICENSE" in r.text
    assert "Version 3, 29 June 2007" in r.text


def test_frontend_has_about_and_license_entry():
    """界面必须提供「关于 / 许可证」入口，并出现版权与许可证名称。"""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="aboutBtn"' in r.text
    assert 'id="aboutBox"' in r.text
    assert "Copyright (C) 2026" in r.text
    assert "GNU General Public License v3.0" in r.text
