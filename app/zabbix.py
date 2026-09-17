"""Zabbix JSON-RPC 数据层。

以"透传用户会话"方式调用 Zabbix API：每个请求携带登录用户的 session id，
因此返回数据天然受该用户的权限过滤，满足团队多人使用的权限隔离。

若 auth.mode == service，则使用服务端固定只读账号（须在环境变量提供账号密码）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx


class ZabbixError(Exception):
    """Zabbix API 返回的 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(f"Zabbix API error {code}: {message}")


class Unauthorized(ZabbixError):
    """会话无效/过期。触发前端重新登录提示。"""


class ZabbixClient:
    def __init__(
        self,
        api_url: str,
        *,
        service_user: Optional[str] = None,
        service_password: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url
        self.service_user = service_user
        self.service_password = service_password
        self.timeout = timeout
        self._service_token: Optional[str] = None

    def _auth_for(self, user_session: Optional[str], auth_mode: str) -> Optional[str]:
        """决定向 API 传哪个 auth token。"""
        if auth_mode == "service":
            if not self._service_token:
                self._service_token = self._login_service()
            return self._service_token
        # user 模式：直接透传前端会话 cookie 里的 sessionid
        return user_session

    def _login_service(self) -> str:
        """服务账号登录，返回 api token。"""
        if not (self.service_user and self.service_password):
            raise ZabbixError(0, "auth.mode=service 但未提供服务账号")
        result = self.call(
            "user.login",
            {"username": self.service_user, "password": self.service_password},
            user_session=None,
            auth_mode="service",
            skip_service_login=True,
        )
        return result

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        user_session: Optional[str] = None,
        auth_mode: str = "user",
        skip_service_login: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": 1,
        }
        auth = self._auth_for(user_session, auth_mode)
        if auth:
            payload["auth"] = auth

        try:
            resp = httpx.post(
                self.api_url, json=payload, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise ZabbixError(0, f"Zabbix 连接失败: {exc}") from exc

        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise ZabbixError(resp.status_code, "Zabbix 返回非 JSON") from exc

        if "error" in body:
            err = body["error"]
            code = int(err.get("code", -1))
            msg = err.get("message", "")
            if code in (-32602, 101, 102, -32700) or "session" in str(msg).lower():
                raise Unauthorized(code, msg, err.get("data"))
            raise ZabbixError(code, msg, err.get("data"))
        return body.get("result")

    # ---- 常用查询 ----
    def user_get(self, sessionid: str) -> Optional[Dict[str, Any]]:
        """用会话 token 取当前用户；无效则抛 Unauthorized。"""
        return self.call(
            "user.get",
            {"output": ["userid", "username", "roleid"]},
            user_session=sessionid,
        )

    def problems(
        self,
        sessionid: str,
        *,
        time_from: int,
        time_till: int,
        limit: int = 100,
        hostids: Optional[list] = None,
    ) -> list:
        params: Dict[str, Any] = {
            "output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged"],
            "time_from": time_from,
            "time_till": time_till,
            # 注意：problem.get 只允许按 eventid 排序；用 clock 会得到
            # "Sorting by field \"clock\" not allowed."（JSON-RPC -32500）
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": limit,
            # 注意：problem.get **不支持** selectHosts —— CProblem.php 只认
            # selectAcknowledges / selectSuppressionData / selectTags，
            # 传 selectHosts 会被静默忽略（不报错，但返回里没有 hosts 字段）。
            # 主机名必须用 objectid(=triggerid) 走 hosts_for_triggers() 反查。
        }
        if hostids:
            # hostids 是 problem.get 真实支持的过滤字段（CProblem.php 用 i.hostid
            # 过滤，源码已核对），用于回答"某台主机当前有哪些告警"。
            params["hostids"] = [str(h) for h in hostids]
        return self.call("problem.get", params, user_session=sessionid)

    def hosts_for_triggers(self, sessionid: str, triggerids: list) -> Dict[str, list]:
        """由 triggerid 反查主机名，返回 {triggerid: [主机名, ...]}。

        存在的唯一理由：problem.get 不返回主机。没有这一步，"现在有哪些告警"
        就答不出"是哪台机器在报警"，模型只能从触发器名字里猜。
        """
        ids = [str(t) for t in triggerids if t]
        if not ids:
            return {}
        rows = self.call(
            "trigger.get",
            {
                "output": ["triggerid"],
                "triggerids": ids,
                "selectHosts": ["hostid", "name"],
            },
            user_session=sessionid,
        )
        return {
            str(r.get("triggerid")): [h.get("name") for h in (r.get("hosts") or [])]
            for r in rows
        }

    def hosts(self, sessionid: str, *, search: Optional[str] = None, limit: int = 200) -> list:
        params: Dict[str, Any] = {
            "output": ["hostid", "name", "status", "available"],
            # 带上接口，才能回答「某台主机的 IP 是多少」这类问题
            "selectInterfaces": [
                "ip", "dns", "port", "type", "main", "useip", "available",
            ],
            "limit": limit,
        }
        if search:
            # 同时匹配显示名与技术名：visible name 与 host 不一致时避免查不到
            params["search"] = {"name": search, "host": search}
            params["searchByAny"] = True
        return self.call("host.get", params, user_session=sessionid)

    def history(
        self,
        sessionid: str,
        itemids: list,
        *,
        value_type: int,
        time_from: int,
        time_till: int,
        limit: int = 500,
    ) -> list:
        return self.call(
            "history.get",
            {
                "output": "extend",
                "history": int(value_type),
                "itemids": itemids,
                "time_from": time_from,
                "time_till": time_till,
                "sortfield": "clock",
                "sortorder": "ASC",
                "limit": limit,
            },
            user_session=sessionid,
        )

    def trends(self, sessionid: str, itemids: list, *, time_from: int, time_till: int) -> list:
        return self.call(
            "trend.get",
            {
                "output": ["itemid", "clock", "value_min", "value_avg", "value_max"],
                "itemids": itemids,
                "time_from": time_from,
                "time_till": time_till,
                "limit": 5000,
            },
            user_session=sessionid,
        )

    def hosts_by_name(self, sessionid: str, name: str) -> list:
        return self.call(
            "host.get",
            {"output": ["hostid", "name"], "search": {"name": name}, "limit": 20},
            user_session=sessionid,
        )

    def items_by_host(
        self,
        sessionid: str,
        hostid: str,
        key_search: Optional[str] = None,
        *,
        enabled_only: bool = False,
        limit: int = 300,
    ) -> list:
        """取某主机的监控项（含"能不能用"的信息）。

        limit 给到 300 而不是 50：生产上单台 SNMP 交换机的接口类监控项就有 150+ 个
        （实测 DEMO_SDWAN02 有 188 项、DEMO_SDWAN01 有 198 项）。取 50 会把真实
        存在的 key 截断掉，模型于是"怎么查都说没有"。

        output 额外带 status/flags/lastclock：
        - status=1 说明该项已禁用（不会有新数据）——必须让模型看得见，
          否则它无法解释"为什么这项没数据"；
        - lastclock 用于判断该项到底有没有采集到过数据；
        - flags 用于剔除 LLD 规则/原型（item.get 默认已排除，这里再兜一层）。
        """
        params: Dict[str, Any] = {
            "output": [
                "itemid",
                "name",
                "key_",
                "units",
                "lastvalue",
                "value_type",
                "status",
                "flags",
                "lastclock",
            ],
            "hostids": [hostid],
            "limit": limit,
        }
        if key_search:
            # 同时按 key 与监控项名称搜（searchByAny=OR，已在 CItem.php 核对支持）。
            # 模型经常拿 "eth0"、"Bits received" 这类**名称**来搜，只搜 key_ 会什么都找不到。
            params["search"] = {"key_": key_search, "name": key_search}
            params["searchByAny"] = True
        if enabled_only:
            params["filter"] = {"status": 0}
        return self.call("item.get", params, user_session=sessionid)

    # ---- 事件历史（可查出已恢复的故障）----
    # 说明：problem.get 只返回"未恢复"的问题（其 SQL 为 r_eventid IS NULL）；
    # 带 recent=true 时也只额外包含"最近 OK_PERIOD 内恢复"的问题，
    # 而生产上 OK_PERIOD 往往是 5m，远不足以回答"今天上午有哪些故障"。
    # 因此查历史故障必须走 event.get：它按时间窗返回全部问题事件，
    # 并通过 r_eventid（未恢复为 "0"）给出恢复关联。
    def events(
        self,
        sessionid: str,
        *,
        time_from: int,
        time_till: int,
        value: int = 1,
        limit: int = 500,
        hostids: Optional[list] = None,
    ) -> list:
        params: Dict[str, Any] = {
            "output": [
                "eventid",
                "clock",
                "name",
                "severity",
                "acknowledged",
                "r_eventid",
                "objectid",
            ],
            "source": 0,  # EVENT_SOURCE_TRIGGERS
            "object": 0,  # EVENT_OBJECT_TRIGGER
            "value": value,  # 1 = 问题事件，0 = 恢复事件
            "time_from": time_from,
            "time_till": time_till,
            # 按 eventid 排序（eventid 单调递增，等价于时间序）；
            # 注意 event.get 与 problem.get 一样不保证支持按 clock 排序
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": limit,
            "selectHosts": ["hostid", "name"],
        }
        if hostids:
            # event.get 支持 hostids（按主机过滤事件），用于回答
            # "某台主机本月有哪些故障"；不传则是全量。
            params["hostids"] = [str(h) for h in hostids]
        return self.call("event.get", params, user_session=sessionid)

    def events_by_ids(self, sessionid: str, eventids: list) -> list:
        """按 eventid 取事件（用于把 r_eventid 换成恢复时间）。"""
        if not eventids:
            return []
        return self.call(
            "event.get",
            {
                "output": ["eventid", "clock", "value"],
                "eventids": eventids,
                "source": 0,
                "object": 0,
            },
            user_session=sessionid,
        )

    def events_window_all(
        self,
        sessionid: str,
        *,
        time_from: int,
        time_till: int,
        page_size: int = 500,
        max_events: int = 20000,
    ) -> tuple:
        """分页取回时间窗内的**全部**问题事件，用于服务端统计（TOP N 等）。

        单次 event.get 有 limit 上限，若只取前 N 条再让模型统计，会得到
        完全错误的排名（真实事故：限 10 条后模型据此报"最多 3 次"，
        而实际有主机高达 40 次）。因此这里按 eventid 倒序翻页取全。

        返回 (events, truncated)；truncated=True 表示达到 max_events 上限，
        结果可能不完整，调用方应显式告知用户。
        """
        collected: list = []
        eventid_till = None
        truncated = False

        while True:
            params: Dict[str, Any] = {
                "output": ["eventid", "clock", "name", "severity", "acknowledged", "r_eventid"],
                "source": 0,
                "object": 0,
                "value": 1,
                "time_from": time_from,
                "time_till": time_till,
                "sortfield": ["eventid"],
                "sortorder": "DESC",
                "limit": page_size,
                "selectHosts": ["hostid", "name"],
            }
            if eventid_till is not None:
                params["eventid_till"] = eventid_till

            batch = self.call("event.get", params, user_session=sessionid) or []
            collected.extend(batch)

            if len(batch) < page_size:
                break
            if len(collected) >= max_events:
                truncated = True
                break
            eventid_till = int(batch[-1]["eventid"]) - 1

        return collected, truncated