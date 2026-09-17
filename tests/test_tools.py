"""工具层测试：白名单校验、参数校验、趋势外推、钉钉安全设置透传（均不打网络）。"""
from __future__ import annotations

import inspect
import json

import pytest

from app.config import SupplierTarget
from app.dingtalk import DingtalkError
from app.tools import (
    ToolContext,
    build_tool_handlers,
    build_tool_schemas,
    tool_query_hosts,
    tool_query_metrics,
    tool_query_problems,
    tool_send_dingtalk,
    tool_trend_analysis,
)
from app.zabbix import ZabbixClient

SUPPLIERS = {
    "网络供应商A": SupplierTarget(
        name="网络供应商A",
        webhook="https://127.0.0.1:1/robot/send?access_token=T",
        keyword="告警",
    )
}

TOOL_NAMES = [
    "query_problems",
    "query_top",
    "query_metrics",
    "query_hosts",
    "trend_analysis",
]


class FakeZabbix:
    def __init__(
        self,
        hosts=None,
        items=None,
        trends=None,
        problems=None,
        history=None,
        events=None,
        events_by_id=None,
        trigger_hosts=None,
    ):
        self._hosts = hosts if hosts is not None else []
        self._trigger_hosts = trigger_hosts or {}
        self._items = items if items is not None else []
        self._trends = trends if trends is not None else []
        self._problems = problems if problems is not None else []
        self._history = history if history is not None else []
        self._events = events if events is not None else []
        self._events_by_id = events_by_id if events_by_id is not None else []
        self.last_window = None

    def hosts_by_name(self, sessionid, name):
        return self._hosts

    def hosts(self, sessionid, search=None, limit=200):
        return self._hosts

    def hosts_for_triggers(self, sessionid, triggerids):
        return {str(k): v for k, v in (self._trigger_hosts or {}).items()}

    def items_by_host(self, sessionid, hostid, key_search=None, *, enabled_only=False, limit=300):
        """与真实客户端同签名，并且真的做过滤。

        如果这里无脑返回全部监控项，"key 猜错时给不出真实清单"这类缺陷
        在单元测试里就会被掩盖掉。
        """
        self.last_items_query = {
            "key_search": key_search,
            "enabled_only": enabled_only,
            "limit": limit,
        }
        rows = list(self._items)
        if key_search:
            rows = [it for it in rows if key_search.lower() in str(it.get("key_", "")).lower()]
        if enabled_only:
            rows = [it for it in rows if str(it.get("status", "0")) != "1"]
        return rows

    def trends(self, sessionid, itemids, time_from, time_till):
        return self._trends

    def history(self, sessionid, itemids, *, value_type, time_from, time_till, limit=500):
        self.last_history_query = {"value_type": value_type, "limit": limit}
        return self._history

    def problems(self, sessionid, time_from, time_till, limit=100, hostids=None):
        self.last_problems_query = {"limit": limit, "hostids": hostids}
        return self._problems

    def events(self, sessionid, *, time_from, time_till, value=1, limit=500, hostids=None):
        self.last_window = (time_from, time_till)
        self.last_events_query = {"limit": limit, "hostids": hostids}
        return self._events

    def events_by_ids(self, sessionid, eventids):
        return self._events_by_id

    def events_window_all(self, sessionid, *, time_from, time_till,
                          page_size=500, max_events=20000):
        self.last_window = (time_from, time_till)
        events = self._events
        truncated = bool(getattr(self, "force_truncated", False))
        if truncated:
            events = events[: max(1, len(events) // 2)]
        return events, truncated


def make_ctx(zabbix=None, suppliers=None, audit=None) -> ToolContext:
    return ToolContext(
        sessionid="sess",
        username="alice",
        zclient=zabbix or FakeZabbix(),
        suppliers=SUPPLIERS if suppliers is None else suppliers,
        audit=audit or (lambda *args: None),
    )


# ---- 注册表 ----

def test_registered_tools_match_expected_set():
    assert [s["name"] for s in build_tool_schemas()] == TOOL_NAMES


def test_send_dingtalk_is_not_exposed_to_llm():
    """发送必须走确定性斜杠命令，绝不能是 LLM 可自主调用的工具。

    真实事故：问「今天上海的天气怎么样？」时，模型自主调用了 send_dingtalk，
    把与监控无关的内容真的发到了供应商钉钉群。
    """
    assert "send_dingtalk" not in [s["name"] for s in build_tool_schemas()]
    assert "send_dingtalk" not in build_tool_handlers(make_ctx())


# ---- query_top：服务端统计 TOP N ----

def test_query_top_aggregates_by_host_and_sorts_desc():
    """复现本次事故：模型在截断清单上数出"最多3次"，实际有主机 40 次。

    统计必须由服务端做，且要扫描该时段全部事件。
    """
    from app.tools import tool_query_top

    def ev(eid, host, resolved=False, trig="Unavailable by ICMP ping", sev="4"):
        return {
            "eventid": str(eid),
            "clock": "1700000000",
            "name": trig,
            "severity": sev,
            "acknowledged": "0",
            "r_eventid": "9" if resolved else "0",
            "hosts": [{"hostid": "1", "name": host}],
        }

    z = FakeZabbix(
        events=[
            ev(1, "BluE_SDWAN01"), ev(2, "BluE_SDWAN01"), ev(3, "BluE_SDWAN01"),
            ev(4, "DEMO_IDC02", resolved=True, trig="TCP8080", sev="2"),
            ev(5, "ATCC_HZ_AF01_LAN"),
            ev(6, "ATCC_HZ_AF01_LAN", resolved=True),
        ]
    )
    out = tool_query_top(make_ctx(z), {"period": "this_month", "top": 10})

    assert out["scanned_events"] == 6
    assert out["distinct_hosts"] == 3
    assert out["active"] == 4
    assert out["resolved"] == 2

    assert [h["host"] for h in out["top_hosts"]] == [
        "BluE_SDWAN01",
        "ATCC_HZ_AF01_LAN",
        "DEMO_IDC02",
    ]
    top = out["top_hosts"][0]
    assert top["count"] == 3
    assert top["active"] == 3
    assert top["resolved"] == 0
    second = out["top_hosts"][1]
    assert second["count"] == 2
    assert second["active"] == 1 and second["resolved"] == 1

    assert out["top_triggers"][0] == {"trigger": "Unavailable by ICMP ping", "count": 5}
    assert out["severity_distribution"]["严重"] == 5


def test_query_top_respects_top_n():
    from app.tools import tool_query_top

    z = FakeZabbix(
        events=[
            {"eventid": str(i), "clock": "1", "name": "t", "severity": "1",
             "acknowledged": "0", "r_eventid": "0",
             "hosts": [{"hostid": "1", "name": f"h{i}"}]}
            for i in range(5)
        ]
    )
    out = tool_query_top(make_ctx(z), {"period": "this_month", "top": 3})
    assert len(out["top_hosts"]) == 3
    assert out["distinct_hosts"] == 5


def test_query_top_handles_events_without_host():
    from app.tools import tool_query_top

    z = FakeZabbix(
        events=[{"eventid": "1", "clock": "1", "name": "t", "severity": "1",
                 "acknowledged": "0", "r_eventid": "0", "hosts": []}]
    )
    out = tool_query_top(make_ctx(z), {"period": "this_month"})
    assert out["top_hosts"][0]["host"] == "(未指定主机)"


def test_query_top_warns_when_truncated():
    from app.tools import tool_query_top

    z = FakeZabbix(
        events=[
            {"eventid": str(i), "clock": "1", "name": "t", "severity": "1",
             "acknowledged": "0", "r_eventid": "0",
             "hosts": [{"hostid": "1", "name": "h"}]}
            for i in range(4)
        ]
    )
    z.force_truncated = True
    out = tool_query_top(make_ctx(z), {"period": "this_month"})
    assert out["truncated"] is True
    assert "warning" in out


def test_query_top_empty_window_gives_hint():
    from app.tools import tool_query_top

    out = tool_query_top(make_ctx(FakeZabbix(events=[])), {"period": "morning"})
    assert out["scanned_events"] == 0
    assert "hint" in out


def test_period_this_month_starts_at_first_day():
    from datetime import datetime

    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("this_month", 30)
    assert datetime.fromtimestamp(t_from).day == 1
    assert datetime.fromtimestamp(t_from).hour == 0
    assert t_till >= t_from
    assert "本月" in desc


def test_period_last_month_covers_full_previous_month():
    from datetime import datetime

    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("last_month", 30)
    start = datetime.fromtimestamp(t_from)
    end = datetime.fromtimestamp(t_till)
    assert start.day == 1 and start.hour == 0
    assert end.day == 1 and end.hour == 0
    # 结束点应是本月 1 日，起点早于结束点
    assert (end.year, end.month) == (
        (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    )
    assert "上月" in desc


def test_every_schema_has_handler():
    handlers = build_tool_handlers(make_ctx())
    for schema in build_tool_schemas():
        assert schema["name"] in handlers


def test_schemas_are_openai_object_shape():
    for schema in build_tool_schemas():
        assert schema["parameters"]["type"] == "object"
        assert "description" in schema


def test_send_dingtalk_must_be_sync():
    """工具调用链是同步的；若为协程函数，handler 会返回 coroutine 导致序列化失败。"""
    assert not inspect.iscoroutinefunction(tool_send_dingtalk)


def test_handlers_return_sync_json_serializable_results(monkeypatch):
    """回归测试：曾因 send_dingtalk 为 async，导致该工具在真实对话中永远失败。

    单元测试若直接 await 工具函数便无法发现，因此这里必须经 handler 管道验证。
    """
    monkeypatch.setattr("app.tools.send_text", lambda *a, **k: {"errcode": 0})
    handlers = build_tool_handlers(make_ctx())
    args_by_tool = {
        "query_problems": {},
        "query_top": {"period": "this_month"},
        "query_metrics": {"host": "h"},
        "query_hosts": {},
        "trend_analysis": {"host": "h"},
    }
    for name, handler in handlers.items():
        out = handler(args_by_tool[name])
        assert not inspect.iscoroutine(out), f"{name} 返回了 coroutine"
        json.dumps(out, ensure_ascii=False)  # 必须可序列化


# ---- query_problems ----


def test_query_problems_current_resolves_hosts_via_trigger():
    """回归：problem.get 不支持 selectHosts，主机名必须由 triggerid 反查。

    修复前 hosts 恒为 []，模型只能从触发器名字猜是哪台机器在报警。
    """
    z = FakeZabbix(
        problems=[
            {
                "eventid": "1",
                "objectid": "9001",
                "name": "CPU 使用率过高",
                "severity": "4",
                "clock": "1700000000",
                "acknowledged": "0",
            }
        ],
        trigger_hosts={"9001": ["web01"]},
    )
    out = tool_query_problems(make_ctx(z), {"scope": "current"})
    assert out["problems"][0]["hosts"] == ["web01"]


def test_query_problems_current_survives_trigger_lookup_failure():
    """反查主机失败不应让整个查询报错，退化为空主机即可。"""

    class Boom(FakeZabbix):
        def hosts_for_triggers(self, sessionid, triggerids):
            from app.zabbix import ZabbixError

            raise ZabbixError(-1, "boom")

    z = Boom(
        problems=[
            {
                "eventid": "1",
                "objectid": "9001",
                "name": "CPU 使用率过高",
                "severity": "4",
                "clock": "1700000000",
                "acknowledged": "0",
            }
        ]
    )
    out = tool_query_problems(make_ctx(z), {"scope": "current"})
    assert out["count"] == 1
    assert out["problems"][0]["hosts"] == []


def test_query_problems_current_scope_formats_severity_label():
    """scope=current（不传时间段）→ 仅当前未恢复。"""
    z = FakeZabbix(
        problems=[
            {
                "eventid": "1",
                "objectid": "9001",
                "name": "CPU high",
                "severity": "4",
                "clock": "1700000000",
                "acknowledged": "0",
            }
        ],
        # 主机名来自 trigger 反查，不是 problem.get 的返回
        # （problem.get 根本不支持 selectHosts）
        trigger_hosts={"9001": ["web01"]},
    )
    out = tool_query_problems(make_ctx(z), {"scope": "current", "limit": 10})
    assert out["scope"] == "current"
    assert out["count"] == 1
    assert out["problems"][0]["severity"] == "严重"
    assert out["problems"][0]["ack"] is False
    assert out["problems"][0]["state"] == "未恢复"
    assert out["problems"][0]["hosts"] == ["web01"]


def test_time_window_always_includes_resolved_even_if_scope_current():
    """关键回归：用户问"今天有哪些告警"时模型常传 scope=current + period，

    此时不能退化成"只查未恢复"——必须返回该时段全部故障（含已恢复）。
    真实事故：模型传 period=today&scope=current，只回了 1 条未恢复的。
    """
    z = FakeZabbix(
        problems=[{"eventid": "1", "name": "still-active", "severity": "4",
                   "clock": "1700000000", "acknowledged": "0", "hosts": []}],
        events=[
            {"eventid": "10", "clock": "1700000000", "name": "resolved-one",
             "severity": "2", "acknowledged": "0", "r_eventid": "100", "hosts": []},
            {"eventid": "11", "clock": "1700000100", "name": "active-one",
             "severity": "4", "acknowledged": "0", "r_eventid": "0", "hosts": []},
        ],
        events_by_id=[{"eventid": "100", "clock": "1700001000", "value": "0"}],
    )
    out = tool_query_problems(
        make_ctx(z), {"period": "today", "scope": "current"}
    )
    assert out["scope"] == "history"          # 时间段优先，忽略 current
    assert out["count"] == 2
    assert out["resolved"] == 1
    assert out["active"] == 1
    names = {r["name"] for r in out["problems"]}
    assert names == {"resolved-one", "active-one"}


def test_days_also_is_treated_as_time_window():
    """days 与 period 同为时间范围，同样应包含已恢复。"""
    z = FakeZabbix(
        events=[{"eventid": "1", "clock": "1700000000", "name": "x", "severity": "1",
                 "acknowledged": "0", "r_eventid": "9", "hosts": []}],
        events_by_id=[{"eventid": "9", "clock": "1700000600", "value": "0"}],
    )
    out = tool_query_problems(make_ctx(z), {"days": 3})
    assert out["scope"] == "history"
    assert out["resolved"] == 1


def test_query_problems_marks_acknowledged_true_for_string_one():
    z = FakeZabbix(
        problems=[
            {
                "eventid": "1",
                "name": "CPU high",
                "severity": "2",
                "clock": "1700000000",
                "acknowledged": "1",
            }
        ]
    )
    assert tool_query_problems(make_ctx(z), {})["problems"][0]["ack"] is True


def test_query_problems_empty_result():
    out = tool_query_problems(make_ctx(), {})
    assert out["scope"] == "current"
    assert out["count"] == 0
    assert out["problems"] == []
    # 空结果时给出可操作提示（如何查时间段内的全部故障）
    assert "period" in out["hint"]


# ---- query_problems：历史（含已恢复）----

def test_resolve_period_morning_is_today_0000_to_1200():
    from datetime import datetime

    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("morning", 1)
    d0 = datetime.fromtimestamp(t_from)
    d1 = datetime.fromtimestamp(t_till)
    now = datetime.now()
    assert (d0.year, d0.month, d0.day) == (now.year, now.month, now.day)
    assert (d0.hour, d0.minute) == (0, 0)
    assert d1.hour == 12
    assert "上午" in desc or "00:00" in desc


def test_resolve_period_yesterday_ends_at_today_midnight():
    from datetime import datetime

    from app.tools import resolve_period

    t_from, t_till, _ = resolve_period("yesterday", 1)
    assert datetime.fromtimestamp(t_till).hour == 0
    assert (t_till - t_from) in (86400, 90000)  # 一天（夏令时容差）


def test_resolve_period_falls_back_to_days():
    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("", 3)
    assert t_till - t_from == 3 * 86400
    assert "3 天" in desc


def test_history_scope_includes_resolved_and_active():
    """用户反馈的核心场景：今天上午的故障必须包含已恢复的。"""
    z = FakeZabbix(
        events=[
            {
                "eventid": "3",
                "clock": "1700000300",
                "name": "TCP8080",
                "severity": "2",
                "acknowledged": "0",
                "r_eventid": "30",          # 已恢复
                "hosts": [{"hostid": "1", "name": "demo_idc02"}],
            },
            {
                "eventid": "2",
                "clock": "1700000200",
                "name": "Unavailable by ICMP ping",
                "severity": "4",
                "acknowledged": "1",
                "r_eventid": "0",           # 未恢复
                "hosts": [{"hostid": "2", "name": "web01"}],
            },
        ],
        events_by_id=[{"eventid": "30", "clock": "1700000900", "value": "0"}],
    )
    out = tool_query_problems(make_ctx(z), {"scope": "history", "period": "morning"})

    assert out["scope"] == "history"
    assert out["count"] == 2
    assert out["resolved"] == 1
    assert out["active"] == 1
    assert out["window"]["description"]

    resolved = [r for r in out["problems"] if r["state"] == "已恢复"][0]
    assert resolved["name"] == "TCP8080"
    assert resolved["resolved_at"] is not None
    assert resolved["duration_min"] == pytest.approx(10.0)   # (1700000900-1700000300)/60
    assert resolved["hosts"] == ["demo_idc02"]

    active = [r for r in out["problems"] if r["state"] == "未恢复"][0]
    assert active["resolved_at"] is None
    assert active["hosts"] == ["web01"]
    assert active["ack"] is True


def test_history_scope_uses_resolved_period_as_window():
    z = FakeZabbix(events=[])
    tool_query_problems(make_ctx(z), {"scope": "history", "period": "today"})
    t_from, t_till = z.last_window
    from datetime import datetime

    assert datetime.fromtimestamp(t_from).hour == 0
    assert t_till >= t_from


def test_history_scope_handles_resolved_without_recovery_time():
    """恢复事件已被清理时，仍应标记为已恢复而不是未恢复。"""
    z = FakeZabbix(
        events=[{"eventid": "9", "clock": "1700000000", "name": "old", "severity": "1",
                 "acknowledged": "0", "r_eventid": "99", "hosts": []}],
        events_by_id=[],           # 取不到恢复时间
    )
    out = tool_query_problems(make_ctx(z), {"scope": "history", "period": "last_7d"})
    row = out["problems"][0]
    assert row["state"] == "已恢复"
    assert row["resolved_at"] is None
    assert row["duration_min"] is None
    assert out["resolved"] == 1


# ---- query_metrics ----

def test_query_metrics_requires_host():
    assert "error" in tool_query_metrics(make_ctx(), {})


def test_query_metrics_host_not_found():
    assert "error" in tool_query_metrics(make_ctx(FakeZabbix(hosts=[])), {"host": "不存在"})


def test_query_metrics_returns_items():
    z = FakeZabbix(
        hosts=[{"hostid": "1", "name": "web01"}],
        items=[
            {
                "itemid": "9",
                "name": "CPU util",
                "key_": "system.cpu.util",
                "lastvalue": "12",
                "units": "%",
            }
        ],
    )
    out = tool_query_metrics(make_ctx(z), {"host": "web01", "key": "cpu"})
    assert out["host"] == "web01"
    assert out["items"][0]["key"] == "system.cpu.util"


# ---- query_hosts（回答"监控了哪些主机"/"运行状况"）----

def test_query_hosts_returns_overview_and_rows():
    z = FakeZabbix(
        hosts=[
            {"hostid": "1", "name": "web01", "status": "0", "available": "1"},
            {"hostid": "2", "name": "db01", "status": "0", "available": "2"},
            {"hostid": "3", "name": "old01", "status": "1"},
        ],
        problems=[
            {"eventid": "1", "name": "Disk low", "severity": "4", "clock": "1700000000", "acknowledged": "0"},
            {"eventid": "2", "name": "CPU high", "severity": "2", "clock": "1700000000", "acknowledged": "0"},
        ],
    )
    out = tool_query_hosts(make_ctx(z), {"days": 7})
    ov = out["overview"]
    assert ov["host_count"] == 3
    assert ov["monitored"] == 2
    assert ov["disabled"] == 1
    assert ov["unavailable"] == 1
    assert ov["problems_last_7d"] == 2
    assert ov["problems_by_severity"] == {"严重": 1, "警告": 1}
    assert [h["name"] for h in out["hosts"]] == ["web01", "db01", "old01"]


def test_query_hosts_tolerates_missing_available_field():
    """内部主机（如 Zabbix server）没有接口，available 字段会缺失。"""
    z = FakeZabbix(hosts=[{"hostid": "10084", "name": "Zabbix server", "status": "0"}])
    out = tool_query_hosts(make_ctx(z), {})
    assert out["hosts"][0]["name"] == "Zabbix server"
    assert "available" not in out["hosts"][0]


def test_query_hosts_empty_environment():
    out = tool_query_hosts(make_ctx(), {})
    assert out["overview"]["host_count"] == 0
    assert out["hosts"] == []


def test_query_hosts_includes_interface_ip():
    """问「某台主机的 IP」时，ip/port 必须能带出来——这是模型唯一的数据来源。"""
    z = FakeZabbix(
        hosts=[
            {
                "hostid": "10650",
                "name": "DEMO_CARRIER01",
                "status": "0",
                "available": "0",
                "interfaces": [
                    {
                        "ip": "203.0.113.10",
                        "dns": "",
                        "port": "10050",
                        "type": "1",
                        "main": "1",
                        "useip": "1",
                        "available": "0",
                    }
                ],
            }
        ]
    )
    out = tool_query_hosts(make_ctx(z), {"search": "DEMO_CARRIER01"})
    row = out["hosts"][0]
    assert row["ip"] == "203.0.113.10"
    assert row["port"] == "10050"
    assert row["interface_type"] == "agent"


def test_query_hosts_prefers_main_interface():
    """一台主机有多个接口时，取 main=1 的那个。"""
    z = FakeZabbix(
        hosts=[
            {
                "hostid": "1",
                "name": "multi",
                "status": "0",
                "interfaces": [
                    {"ip": "10.0.0.9", "port": "161", "type": "2", "main": "0", "useip": "1"},
                    {"ip": "10.0.0.1", "port": "10050", "type": "1", "main": "1", "useip": "1"},
                ],
            }
        ]
    )
    out = tool_query_hosts(make_ctx(z), {})
    assert out["hosts"][0]["ip"] == "10.0.0.1"
    assert out["hosts"][0]["interface_type"] == "agent"


def test_query_hosts_uses_dns_when_useip_disabled():
    """useip=0 的主机走 DNS 名，不能把 IP 当成答案给出去。"""
    z = FakeZabbix(
        hosts=[
            {
                "hostid": "1",
                "name": "bydns",
                "status": "0",
                "interfaces": [
                    {
                        "ip": "10.0.0.1",
                        "dns": "web01.example.com",
                        "port": "10050",
                        "type": "1",
                        "main": "1",
                        "useip": "0",
                    }
                ],
            }
        ]
    )
    out = tool_query_hosts(make_ctx(z), {})
    assert out["hosts"][0]["ip"] == "web01.example.com"


def test_query_hosts_omits_ip_when_no_interface():
    """没有接口的主机（如 Zabbix server 自身）不应凭空出现 ip 字段。"""
    z = FakeZabbix(hosts=[{"hostid": "10084", "name": "Zabbix server", "status": "0"}])
    out = tool_query_hosts(make_ctx(z), {})
    assert "ip" not in out["hosts"][0]
    assert "port" not in out["hosts"][0]
    assert "interface_type" not in out["hosts"][0]


def test_zabbix_hosts_requests_interfaces():
    """数据层必须真的把 selectInterfaces 发给 API，否则工具层永远拿不到 IP。"""
    client = ZabbixClient("http://zabbix.example/api_jsonrpc.php")
    captured = {}

    def fake_call(method, params, **kwargs):
        captured["method"] = method
        captured["params"] = params
        return []

    client.call = fake_call
    client.hosts("sess", search="DEMO_CARRIER01")
    assert captured["method"] == "host.get"
    assert "selectInterfaces" in captured["params"]
    # 显示名与技术名都要搜，避免 visible name 与 host 不一致时查不到
    assert captured["params"]["search"] == {
        "name": "DEMO_CARRIER01",
        "host": "DEMO_CARRIER01",
    }
    assert captured["params"]["searchByAny"] is True


# ---- trend_analysis ----

def test_trend_analysis_requires_host():
    assert "error" in tool_trend_analysis(make_ctx(), {})


def test_trend_analysis_reports_insufficient_data():
    z = FakeZabbix(
        hosts=[{"hostid": "1", "name": "web01"}],
        items=[{"itemid": "9", "name": "disk", "key_": "vfs.fs.size", "units": "B"}],
        trends=[],
        history=[],
    )
    out = tool_trend_analysis(make_ctx(z), {"host": "web01", "key": "vfs.fs.size"})
    assert "error" in out
    # 失败也必须给出"下一步怎么办"，不能是死胡同
    assert "hint" in out
    assert "tried_keys" in out


def test_trend_analysis_uses_trend_table_and_projects_days_to_full():
    base = 1_700_000_000
    day = 86400
    z = FakeZabbix(
        hosts=[{"hostid": "1", "name": "db01"}],
        items=[
            {"itemid": "9", "name": "disk used", "key_": "vfs.fs.size[/,used]", "units": "B"}
        ],
        trends=[
            {"clock": str(base - 14 * day), "value_avg": "100",
             "value_min": "90", "value_max": "110"},
            {"clock": str(base), "value_avg": "240",
             "value_min": "230", "value_max": "250"},
        ],
    )
    out = tool_trend_analysis(
        make_ctx(z), {"host": "db01", "key": "vfs.fs.size", "days": 14, "total": 500}
    )
    a = out["analysis"]
    assert a["source"] == "trend"
    assert a["daily_growth"] == pytest.approx(10.0)
    assert a["days_to_full"] == pytest.approx(26.0)
    assert a["item"] == "disk used"
    # 趋势表按小时给 min/avg/max，窗口统计要如实汇总而不是只报均值
    assert a["min"] == pytest.approx(90.0)
    assert a["max"] == pytest.approx(250.0)


def test_trend_analysis_without_total_omits_projection():
    base = 1_700_000_000
    day = 86400
    z = FakeZabbix(
        hosts=[{"hostid": "1", "name": "db01"}],
        items=[{"itemid": "9", "name": "disk", "key_": "vfs.fs.size", "units": "B"}],
        trends=[
            {"clock": str(base - 7 * day), "value_avg": "10"},
            {"clock": str(base), "value_avg": "80"},
        ],
    )
    out = tool_trend_analysis(make_ctx(z), {"host": "db01", "key": "vfs.fs.size"})
    assert out["analysis"]["days_to_full"] is None


def test_trend_analysis_falls_back_to_history_when_trend_table_empty():
    """Zabbix 按整点写趋势表；新装环境趋势为空时应回退到原始历史数据。"""
    base = 1_700_000_000
    day = 86400
    z = FakeZabbix(
        hosts=[{"hostid": "1", "name": "db01"}],
        items=[
            {
                "itemid": "9",
                "name": "disk used",
                "key_": "vfs.fs.size[/,used]",
                "units": "B",
                "value_type": "3",
            }
        ],
        trends=[],
        history=[
            {"clock": str(base - 2 * day), "value": "100"},
            {"clock": str(base), "value": "140"},
        ],
    )
    out = tool_trend_analysis(
        make_ctx(z), {"host": "db01", "key": "vfs.fs.size", "days": 7, "total": 300}
    )
    a = out["analysis"]
    assert a["source"] == "history"
    assert a["daily_growth"] == pytest.approx(20.0)
    assert a["days_to_full"] == pytest.approx(8.0)
    assert "note" in a


# ---- send_dingtalk：白名单与校验（不打网络）----

def test_send_dingtalk_requires_supplier_and_lists_whitelist():
    out = tool_send_dingtalk(make_ctx(), {"message": "hi"})
    assert "error" in out
    assert "网络供应商A" in out["error"]


def test_send_dingtalk_rejects_unregistered_supplier():
    out = tool_send_dingtalk(make_ctx(), {"supplier": "恶意供应商", "message": "hi"})
    assert "error" in out
    assert "网络供应商A" in out["error"]


def test_send_dingtalk_requires_non_empty_message():
    out = tool_send_dingtalk(make_ctx(), {"supplier": "网络供应商A", "message": ""})
    assert out["error"] == "message 不能为空"


def test_send_dingtalk_rejects_whitespace_only_message():
    out = tool_send_dingtalk(make_ctx(), {"supplier": "网络供应商A", "message": "   "})
    assert out["error"] == "message 不能为空"


def test_send_dingtalk_prefix_resolves_supplier_before_message_check():
    out = tool_send_dingtalk(make_ctx(), {"supplier": "网络", "message": ""})
    assert out["error"] == "message 不能为空"


def test_send_dingtalk_refuses_when_whitelist_empty():
    out = tool_send_dingtalk(make_ctx(suppliers={}), {"supplier": "任意", "message": "hi"})
    assert "error" in out


def test_no_audit_written_when_message_empty():
    recorded = []
    out = tool_send_dingtalk(
        make_ctx(audit=lambda *a: recorded.append(a)),
        {"supplier": "网络供应商A", "message": ""},
    )
    assert "error" in out
    assert recorded == []


# ---- send_dingtalk：安全设置透传与审计（mock 掉真实发送）----

def test_send_dingtalk_passes_secret_and_keyword(monkeypatch):
    captured = {}

    def fake_send_text(webhook, content, **kwargs):
        captured["webhook"] = webhook
        captured["content"] = content
        captured.update(kwargs)
        return {"errcode": 0}

    monkeypatch.setattr("app.tools.send_text", fake_send_text)
    target = SupplierTarget(
        name="供应商X", webhook="https://hook/x", secret="SEC1", keyword="告警"
    )
    out = tool_send_dingtalk(
        make_ctx(suppliers={"供应商X": target}),
        {"supplier": "供应商X", "message": "磁盘要满了"},
    )
    assert out["ok"] is True
    assert out["supplier"] == "供应商X"
    assert captured["webhook"] == "https://hook/x"
    assert captured["secret"] == "SEC1"
    assert captured["keyword"] == "告警"
    assert "磁盘要满了" in captured["content"]


def test_send_dingtalk_success_writes_ok_audit(monkeypatch):
    monkeypatch.setattr("app.tools.send_text", lambda *a, **k: {"errcode": 0})
    recorded = []
    out = tool_send_dingtalk(
        make_ctx(audit=lambda *a: recorded.append(a)),
        {"supplier": "网络供应商A", "message": "出口丢包"},
    )
    assert out["ok"] is True
    assert len(recorded) == 1
    username, action, target_name, detail = recorded[0]
    assert username == "alice"
    assert action == "send_dingtalk"
    assert target_name == "网络供应商A"
    assert detail.startswith("OK:")


def test_send_dingtalk_failure_writes_failed_audit_and_returns_error(monkeypatch):
    def boom(*args, **kwargs):
        raise DingtalkError("钉钉返回错误 errcode=310000 errmsg=keywords not in content")

    monkeypatch.setattr("app.tools.send_text", boom)
    recorded = []
    out = tool_send_dingtalk(
        make_ctx(audit=lambda *a: recorded.append(a)),
        {"supplier": "网络供应商A", "message": "出口丢包"},
    )
    assert "error" in out
    assert "310000" in out["error"]
    assert len(recorded) == 1
    assert recorded[0][3].startswith("FAILED")


def test_send_dingtalk_empty_webhook_reports_error(monkeypatch):
    monkeypatch.setattr(
        "app.tools.send_text",
        lambda *a, **k: (_ for _ in ()).throw(DingtalkError("供应商 webhook 未配置")),
    )
    out = tool_send_dingtalk(
        make_ctx(suppliers={"空webhook": SupplierTarget(name="空webhook", webhook="")}),
        {"supplier": "空webhook", "message": "hi"},
    )
    assert "error" in out
    assert "webhook 未配置" in out["error"]


# ---- 钉钉消息模板（可配置）----

def _capture(monkeypatch) -> dict:
    captured = {}

    def fake_send_text(webhook, content, **kwargs):
        captured["content"] = content
        return {"errcode": 0}

    monkeypatch.setattr("app.tools.send_text", fake_send_text)
    return captured


def test_default_template_includes_username_and_message(monkeypatch):
    captured = _capture(monkeypatch)
    tool_send_dingtalk(make_ctx(), {"supplier": "网络供应商A", "message": "出口丢包"})
    assert captured["content"] == "[zabbix-AI] alice 上报：\n出口丢包"


def test_global_template_override(monkeypatch):
    """可通过全局配置去掉发送人前缀——即用户反馈的"Admin 上报"那段。"""
    captured = _capture(monkeypatch)
    ctx = make_ctx()
    ctx.message_template = "{message}"
    tool_send_dingtalk(ctx, {"supplier": "网络供应商A", "message": "出口丢包"})
    assert captured["content"] == "出口丢包"


def test_supplier_level_template_wins_over_global(monkeypatch):
    captured = _capture(monkeypatch)
    target = SupplierTarget(
        name="网络供应商A",
        webhook="https://hook/a",
        message_template="【来自监控】{message}",
    )
    ctx = make_ctx(suppliers={"网络供应商A": target})
    ctx.message_template = "全局模板：{username} - {message}"
    tool_send_dingtalk(ctx, {"supplier": "网络供应商A", "message": "出口丢包"})
    assert captured["content"] == "【来自监控】出口丢包"


def test_template_keeps_braces_inside_message(monkeypatch):
    """正文里出现花括号不应被当作占位符破坏。"""
    captured = _capture(monkeypatch)
    ctx = make_ctx()
    ctx.message_template = "{message}"
    tool_send_dingtalk(
        ctx, {"supplier": "网络供应商A", "message": 'JSON {"a": 1} 与 {unknown}'}
    )
    assert captured["content"] == 'JSON {"a": 1} 与 {unknown}'


def test_unknown_placeholder_is_left_untouched(monkeypatch):
    captured = _capture(monkeypatch)
    ctx = make_ctx()
    ctx.message_template = "[{unknown}] {message}"
    tool_send_dingtalk(ctx, {"supplier": "网络供应商A", "message": "hi"})
    assert captured["content"] == "[{unknown}] hi"


def test_username_containing_placeholder_is_not_resubstituted(monkeypatch):
    """单次替换：用户名里若恰好含 {message}，不应把正文再插一遍。"""
    captured = _capture(monkeypatch)
    ctx = make_ctx()
    ctx.username = "{message}"
    ctx.message_template = "{username}: {message}"
    tool_send_dingtalk(ctx, {"supplier": "网络供应商A", "message": "正文"})
    assert captured["content"] == "{message}: 正文"


# ---- v1.0.4 回归：key 猜错绝不能变成"没有数据" ----
# 生产实测（2026-09-16）：用户问「DEMO_SDWAN01 本月运行状况分析」与
# 「DEMO_SDWAN02 告警时间段的带宽使用情况」，模型连续猜了
# system.cpu.load[0,5,15min] / net.if.incoming.bytes / net.if.outgoing.bytes。
# 这两台其实都是 SNMP 设备（interfaces.type=2），真实 key 是
# net.if.in[ifHCInOctets.15]；而旧实现每次只回 {"items": []}——没有任何线索，
# 模型只能放弃并答复"无法获取数据"。事实是两台主机分别有 198 / 188 个监控项，
# 近 30 天趋势数据各有 2 万多行。**猜不到 key ≠ 没有数据。**

SNMP_ITEMS = [
    {
        "itemid": "101",
        "name": "Interface eth0(): Bits received",
        "key_": "net.if.in[ifHCInOctets.15]",
        "units": "bps",
        "value_type": "3",
        "status": "0",
        "flags": "4",
        "lastvalue": "1234",
        "lastclock": "1700000000",
    },
    {
        "itemid": "102",
        "name": "Interface eth1(): Bits sent",
        "key_": "net.if.out[ifHCOutOctets.16]",
        "units": "bps",
        "value_type": "3",
        "status": "0",
        "flags": "4",
        "lastvalue": "77",
        "lastclock": "1700000000",
    },
    {
        "itemid": "103",
        "name": "Available memory",
        "key_": "vm.memory.available[snmp]",
        "units": "B",
        "value_type": "3",
        "status": "0",
        "flags": "0",
        "lastvalue": "999",
        "lastclock": "1700000000",
    },
    {
        # 禁用项：没有新数据，但必须让模型看得见（否则解释不了"为什么没数据"）
        "itemid": "104",
        "name": "Interface eth9(): Bits received",
        "key_": "net.if.in[ifHCInOctets.99]",
        "units": "bps",
        "value_type": "3",
        "status": "1",
        "flags": "4",
        "lastvalue": "",
        "lastclock": "0",
    },
    {
        # LLD 发现规则：文本型、非数值，不应进入候选
        "itemid": "105",
        "name": "Network interfaces discovery",
        "key_": "net.if.discovery",
        "units": "",
        "value_type": "4",
        "status": "0",
        "flags": "1",
        "lastvalue": "",
        "lastclock": "0",
    },
]

TREND_ROWS = [
    {"clock": "1700000000", "value_avg": "100", "value_min": "80", "value_max": "120"},
    {"clock": "1700003600", "value_avg": "200", "value_min": "150", "value_max": "260"},
]


def snmp_ctx(**kwargs):
    z = FakeZabbix(
        hosts=[{"hostid": "500", "name": "DEMO_SDWAN01"}], items=list(SNMP_ITEMS), **kwargs
    )
    return make_ctx(z), z


def test_key_miss_lists_real_key_families_instead_of_empty_list():
    """核心回归：key 没猜中时必须给出该主机真实存在的 key。"""
    ctx, _z = snmp_ctx()
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01", "key": "fan.speed.rpm"})

    assert out["matched"] == 0
    fams = {f["family"] for f in out["key_families"]}
    assert {"net.if.in", "net.if.out", "vm.memory.available"} <= fams
    assert "hint" in out
    # 要连"真实 key 长什么样"一起给出去，模型才能照抄重试
    assert "net.if.in[ifHCInOctets.15]" in json.dumps(out, ensure_ascii=False)
    # LLD 发现规则不能出现在清单里（它没有数据，会把模型带偏）
    assert "net.if.discovery" not in fams


def test_relaxed_key_hint_matches_snmp_bandwidth_key():
    """net.if.incoming.bytes 不存在 → 自动放宽到 net.if.in，省掉一轮对话。"""
    ctx, _z = snmp_ctx()
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01", "key": "net.if.incoming.bytes"})

    assert out["relaxed_from"] == "net.if.incoming.bytes"
    keys = {it["key"] for it in out["items"]}
    assert "net.if.in[ifHCInOctets.15]" in keys


def test_metrics_marks_disabled_items():
    """禁用项必须带标记，模型才能解释"这项为什么没数据"。"""
    ctx, _z = snmp_ctx()
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01", "key": "net.if.in"})
    rows = {r["key"]: r for r in out["items"]}

    assert rows["net.if.in[ifHCInOctets.15]"]["last"] == "1234"
    assert rows["net.if.in[ifHCInOctets.15]"]["units"] == "bps"
    assert rows["net.if.in[ifHCInOctets.99]"]["status"] == "已禁用"


def test_metrics_window_returns_min_max_avg():
    """「告警时间段的带宽」需要时间段统计；旧实现只给一个 lastvalue。"""
    ctx, _z = snmp_ctx(trends=TREND_ROWS)
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01", "key": "net.if.in", "period": "today"})

    assert out["scope"] == "window"
    assert out["window"]["description"]
    row = out["items"][0]
    assert row["key"] == "net.if.in[ifHCInOctets.15]"   # 最活跃的排最前
    assert row["min"] == pytest.approx(80.0)
    assert row["max"] == pytest.approx(260.0)
    assert row["avg"] == pytest.approx(150.0)
    assert row["first"] == pytest.approx(100.0)
    assert row["last"] == pytest.approx(200.0)
    assert row["source"] == "trend"
    assert row["samples"] == 2


def test_metrics_without_key_returns_key_family_overview():
    """没给 key 时给"按族汇总"，而不是随手丢 20 个接口项。"""
    ctx, _z = snmp_ctx()
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01"})

    assert "key_families" in out
    assert "items" not in out
    # 5 项里排除 LLD 发现规则(flags=1) 后剩 4 项（含 1 个禁用项，用于诊断）
    assert out["items_total"] == 5
    assert out["items_usable"] == 4


def test_metrics_window_without_key_gives_one_item_per_family():
    """「某主机本月运行状况」：每个 key 族取一个代表项做时段统计。"""
    ctx, _z = snmp_ctx(trends=TREND_ROWS)
    out = tool_query_metrics(ctx, {"host": "DEMO_SDWAN01", "period": "this_month"})

    keys = [r["key"] for r in out["items"]]
    assert len(keys) == len(set(keys))          # 每族只取一个
    families = {k.split("[")[0] for k in keys}
    assert families == {"net.if.in", "net.if.out", "vm.memory.available"}
    assert "每个 key 族取了一个代表项" in out["note"]


def test_trend_analysis_skips_disabled_rule_and_text_items():
    """候选必须筛掉禁用项/发现规则，否则永远抽到没数据的项。"""
    ctx, _z = snmp_ctx(history=[
        {"clock": "1699000000", "value": "10"},
        {"clock": "1700000000", "value": "30"},
    ])
    out = tool_trend_analysis(ctx, {"host": "DEMO_SDWAN01", "days": 7})

    dumped = json.dumps(out, ensure_ascii=False)
    assert "net.if.discovery" not in dumped          # LLD 规则（flags=1）
    assert "ifHCInOctets.99" not in dumped           # 禁用项（status=1）
    assert out["candidates_tried"] == 3              # 101 / 102 / 103
    assert len(out["analyses"]) == 3
    assert out["analyses"][0]["source"] == "history"
    assert "note" in out["analyses"][0]


def test_trend_analysis_without_data_suggests_next_step():
    """取不到数据时也要给出可执行的下一步，而不是一句"无法获取"。"""
    ctx, _z = snmp_ctx()
    out = tool_trend_analysis(ctx, {"host": "DEMO_SDWAN01", "key": "net.if.in"})

    assert "error" in out
    assert "hint" in out
    assert "key_families" in out
    assert out["window"]["description"]


def test_query_problems_scopes_to_host_history():
    """「某台主机本月有哪些告警」必须真的把 hostids 传给 event.get。"""
    z = FakeZabbix(
        hosts=[{"hostid": "500", "name": "DEMO_SDWAN01"}],
        events=[
            {"eventid": "1", "clock": "1700000000", "name": "x", "severity": "1",
             "acknowledged": "0", "r_eventid": "0", "hosts": [{"name": "DEMO_SDWAN01"}]}
        ],
    )
    out = tool_query_problems(
        make_ctx(z), {"host": "DEMO_SDWAN01", "period": "this_month"}
    )

    assert z.last_events_query["hostids"] == ["500"]
    assert out["host"] == "DEMO_SDWAN01"
    assert out["count"] == 1


def test_query_problems_scopes_to_host_current():
    z = FakeZabbix(
        hosts=[{"hostid": "500", "name": "DEMO_SDWAN01"}],
        problems=[{"eventid": "1", "objectid": "9001", "name": "x", "severity": "2",
                   "clock": "1700000000", "acknowledged": "0"}],
        trigger_hosts={"9001": ["DEMO_SDWAN01"]},
    )
    out = tool_query_problems(make_ctx(z), {"host": "DEMO_SDWAN01", "scope": "current"})

    assert z.last_problems_query["hostids"] == ["500"]
    assert out["host"] == "DEMO_SDWAN01"
    assert out["problems"][0]["hosts"] == ["DEMO_SDWAN01"]


def test_query_problems_unknown_host_returns_error():
    out = tool_query_problems(
        make_ctx(FakeZabbix(hosts=[])), {"host": "不存在的主机", "period": "today"}
    )
    assert "error" in out


def test_key_families_group_interface_keys():
    from app.tools import _key_families

    fams = {f["family"]: f for f in _key_families(SNMP_ITEMS)}
    assert fams["net.if.in"]["count"] == 2                       # 含 1 个禁用项
    assert fams["net.if.in"]["sample_key"] == "net.if.in[ifHCInOctets.15]"
    assert fams["net.if.out"]["sample_last"] == "77"
    assert fams["vm.memory.available"]["sample_last"] == "999"


def test_zabbix_items_by_host_requests_full_inventory():
    """数据层必须一次拿足监控项：生产单台 SNMP 设备有 150+ 项，
    limit=50 会把真实存在的 key 截断掉，模型就会"怎么查都说没有"。"""
    client = ZabbixClient("http://zabbix.example/api_jsonrpc.php")
    captured = {}

    def fake_call(method, params, **kwargs):
        captured["method"] = method
        captured["params"] = params
        return []

    client.call = fake_call
    client.items_by_host("sess", "500", "net.if.in", enabled_only=True)

    assert captured["method"] == "item.get"
    assert captured["params"]["limit"] >= 200
    assert captured["params"]["filter"] == {"status": 0}
    for field in ("status", "flags", "lastclock"):
        assert field in captured["params"]["output"]


def test_zabbix_items_by_host_searches_item_name_too():
    """模型也常拿监控项**名称**（eth0 / Bits received）来搜，只搜 key_ 会漏掉。"""
    client = ZabbixClient("http://zabbix.example/api_jsonrpc.php")
    captured = {}

    def fake_call(method, params, **kwargs):
        captured["params"] = params
        return []

    client.call = fake_call
    client.items_by_host("sess", "500", "eth0")

    assert captured["params"]["search"] == {"key_": "eth0", "name": "eth0"}
    assert captured["params"]["searchByAny"] is True


# ---- v1.0.6 回归：时段必须被正确解析，不认识也绝不能静默替换 ----
# 生产实测：模型传了 period="noon"，我们没这个时段，悄悄退化成"最近 1 天"，
# 于是它把跨 24 小时的事件（含前一天的）答成了"中午的告警"。

def test_resolve_period_noon_is_11_to_14():
    from datetime import datetime

    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("noon", 1)
    assert datetime.fromtimestamp(t_from).hour == 11
    assert datetime.fromtimestamp(t_till).hour == 14
    assert "中午" in desc


def test_resolve_period_day_parts_cover_whole_day_without_overlap():
    """凌晨+上午+中午+下午+晚上 应覆盖整天（且下午/晚上不再互相吞并）。"""
    from datetime import datetime

    from app.tools import resolve_period

    spans = {k: resolve_period(k, 1) for k in
             ("early_morning", "morning", "noon", "afternoon", "evening")}
    hours = {k: (datetime.fromtimestamp(v[0]).hour, datetime.fromtimestamp(v[1]).hour)
             for k, v in spans.items()}
    assert hours["early_morning"] == (0, 6)
    assert hours["noon"] == (11, 14)
    assert hours["afternoon"] == (12, 18)
    assert hours["evening"] == (18, 0)          # 18:00 到次日 00:00


def test_resolve_period_accepts_chinese_words():
    """模型很自然会直接传中文，不能再让它踩空。"""
    from datetime import datetime

    from app.tools import resolve_period

    cases = {"中午": (11, 14), "凌晨": (0, 6), "下午": (12, 18), "上午": (0, 12)}
    for word, (h0, h1) in cases.items():
        t_from, t_till, desc = resolve_period(word, 1)
        assert datetime.fromtimestamp(t_from).hour == h0, word
        assert datetime.fromtimestamp(t_till).hour == h1, word
        assert word in desc


def test_resolve_period_chinese_with_day_prefix():
    """「昨天下午」必须是昨天 12:00-18:00，不能被当成今天下午。"""
    from datetime import datetime, timedelta

    from app.tools import resolve_period

    t_from, t_till, desc = resolve_period("昨天下午", 1)
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    assert datetime.fromtimestamp(t_from) == today0 - timedelta(days=1) + timedelta(hours=12)
    assert datetime.fromtimestamp(t_till) == today0 - timedelta(days=1) + timedelta(hours=18)
    assert "昨天" in desc


def test_resolve_period_never_silently_swaps_unknown_period():
    """核心回归：不认识的时段必须留下痕迹，而不是悄悄返回别的窗口。"""
    from app.tools import period_supported, resolve_period

    t_from, t_till, desc = resolve_period("喝茶时间", 2)
    assert t_till - t_from == 2 * 86400          # 仍按 days 兜底
    assert "未能识别" in desc and "喝茶时间" in desc   # 但描述里必须说清楚
    assert period_supported("喝茶时间") is False
    assert period_supported("noon") and period_supported("中午") and period_supported("")


def test_query_problems_flags_unrecognized_period():
    from app.tools import tool_query_problems

    out = tool_query_problems(make_ctx(FakeZabbix(events=[])), {"period": "喝茶时间"})
    assert out["period_unrecognized"] is True
    assert "warning" in out
    assert "noon" in out["supported_periods"]


def test_query_problems_flags_truncated_result():
    """静默截断会让模型把"前 N 条"当全量，必须显式标注。"""
    from app.tools import tool_query_problems

    events = [
        {"eventid": str(i), "clock": "1700000000", "name": "t%d" % i, "severity": "1",
         "acknowledged": "0", "r_eventid": "0", "hosts": [{"name": "h"}]}
        for i in range(10)
    ]
    out = tool_query_problems(make_ctx(FakeZabbix(events=events)), {"period": "today", "limit": 10})
    assert out["truncated"] is True
    assert "上限" in out["warning"]


def test_zabbix_events_sends_hostids_only_when_given():
    """不传 host 时不能给 event.get 塞 hostids，否则全量查询会退化成空。"""
    client = ZabbixClient("http://zabbix.example/api_jsonrpc.php")
    captured = {}

    def fake_call(method, params, **kwargs):
        captured["params"] = params
        return []

    client.call = fake_call
    client.events("sess", time_from=1, time_till=2)
    assert "hostids" not in captured["params"]
    client.events("sess", time_from=1, time_till=2, hostids=["500"])
    assert captured["params"]["hostids"] == ["500"]
