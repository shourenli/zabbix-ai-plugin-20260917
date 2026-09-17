#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 问答验证用的确定性 fixture：构造/清理一套可预期的监控数据。

为什么需要它：要验证「AI 回答得对不对」，得先有一份**已知正确答案**的数据。
用 trapper 监控项 + 自实现 sender 协议精确控制取值，就能精确控制触发器何时
触发/恢复，从而得到完全可预期的「当前告警」与「已恢复告警」集合。

环境变量（都有默认值，便于本机或部署机直接跑）：
  QA_ZABBIX_API       Zabbix API 地址，默认 http://127.0.0.1:801/api_jsonrpc.php
  QA_ZABBIX_USER      登录用户，默认 Admin
  QA_ZABBIX_PASSWORD  登录口令，默认 zabbix
  QA_ZABBIX_SERVER    trapper 投递地址，默认 127.0.0.1
  QA_ZABBIX_TRAPPER   trapper 端口，默认 10051
  QA_HOST_PREFIX      fixture 主机名前缀，默认 AIQA_

用法：
  qa_fixture.py setup      创建并制造告警序列（可重复执行，已存在则跳过）
  qa_fixture.py status     打印 fixture 状态与 ground truth
  qa_fixture.py teardown   删除全部 fixture（含主机组）

注意：请只在测试环境运行。它会创建/删除主机。
"""
import json
import os
import socket
import struct
import sys
import time

import httpx

ZABBIX_API = os.environ.get("QA_ZABBIX_API", "http://127.0.0.1:801/api_jsonrpc.php")
ZABBIX_USER = os.environ.get("QA_ZABBIX_USER", "Admin")
ZABBIX_PASSWORD = os.environ.get("QA_ZABBIX_PASSWORD", "zabbix")
TRAPPER_HOST = os.environ.get("QA_ZABBIX_SERVER", "127.0.0.1")
TRAPPER_PORT = int(os.environ.get("QA_ZABBIX_TRAPPER", "10051"))
PREFIX = os.environ.get("QA_HOST_PREFIX", "AIQA_")
GROUP_NAME = os.environ.get("QA_GROUP_NAME", "AI_QA_TEST")

# SNMP 设备：key 命名与 agent 主机完全不同，用来复现生产缺陷
# 「模型猜 net.if.incoming.bytes，而真实 key 是 net.if.in[ifHCInOctets.15]」。
# 同时带一个文本项与一个禁用项，用于验证候选过滤。
SDWAN_HOST = PREFIX + "SDWAN01"
SDWAN_IP = "10.99.9.9"
SDWAN_BW_IN_KEY = "net.if.in[ifHCInOctets.15]"
SDWAN_BW_OUT_KEY = "net.if.out[ifHCOutOctets.15]"
SDWAN_MEM_KEY = "vm.memory.available[snmp]"
SDWAN_TEXT_KEY = "net.if.discovery"           # 文本型，非数值，不得进入趋势候选
SDWAN_DISABLED_KEY = "net.if.in[ifHCInOctets.99]"  # 禁用项，没有数据

HOSTS = [
    {
        "host": PREFIX + "WEB01",
        "ip": "10.99.1.1",
        "items": [("CPU 使用率", "aiqa.cpu", "%"), ("磁盘已用", "aiqa.disk.used", "B")],
    },
    {
        "host": PREFIX + "DB01",
        "ip": "10.99.1.2",
        "items": [("CPU 使用率", "aiqa.cpu", "%"), ("磁盘已用", "aiqa.disk.used", "B")],
    },
    {
        "host": PREFIX + "NOIP",
        "ip": None,  # 无接口主机，用于验证「不应编造 IP」
        "items": [("CPU 使用率", "aiqa.cpu", "%")],
    },
    {
        "host": SDWAN_HOST,
        "ip": SDWAN_IP,
        "if_type": 2,       # SNMP 接口
        "port": "161",
        "items": [
            ("Interface eth0(): Bits received", SDWAN_BW_IN_KEY, "bps"),
            ("Interface eth0(): Bits sent", SDWAN_BW_OUT_KEY, "bps"),
            ("Available memory", SDWAN_MEM_KEY, "B"),
        ],
    },
]

TRIGGERS = [
    (PREFIX + "WEB01 CPU 过高", "last(/%sWEB01/aiqa.cpu)>90" % PREFIX, 4),
    (PREFIX + "DB01 磁盘将满", "last(/%sDB01/aiqa.disk.used)>1000" % PREFIX, 2),
]

# (host, key, value, 说明) —— 制造 3 次 WEB01 告警（末次未恢复）+ 1 次 DB01 告警（已恢复）
SENDS = [
    (PREFIX + "WEB01", "aiqa.cpu", 95, "触发 CPU 告警 #1"),
    (PREFIX + "WEB01", "aiqa.cpu", 10, "恢复 #1"),
    (PREFIX + "WEB01", "aiqa.cpu", 96, "触发 CPU 告警 #2"),
    (PREFIX + "WEB01", "aiqa.cpu", 10, "恢复 #2"),
    (PREFIX + "WEB01", "aiqa.cpu", 97, "触发 CPU 告警 #3（保持未恢复）"),
    (PREFIX + "DB01", "aiqa.disk.used", 2000, "触发磁盘告警 #1"),
    (PREFIX + "DB01", "aiqa.disk.used", 100, "恢复 #1（将成为已恢复告警）"),
]

# SNMP 主机的取值：入向峰值 8192、出向峰值 3072、内存 1 GiB。
# 数字特意取得好认，便于与答案逐字比对。
SDWAN_SENDS = [
    (SDWAN_BW_IN_KEY, 1024, "入向 #1"),
    (SDWAN_BW_OUT_KEY, 512, "出向 #1"),
    (SDWAN_MEM_KEY, 1073741824, "内存"),
    (SDWAN_BW_IN_KEY, 4096, "入向 #2"),
    (SDWAN_BW_OUT_KEY, 2048, "出向 #2"),
    (SDWAN_TEXT_KEY, "ge0,ge1,eth0", "接口发现（文本）"),
    (SDWAN_BW_IN_KEY, 8192, "入向 #3（峰值）"),
    (SDWAN_BW_OUT_KEY, 3072, "出向 #3（峰值）"),
]

# 全部要投递的取值：(host, key, value, 说明)
SEND_PLAN = SENDS + [(SDWAN_HOST, k, v, "[SDWAN] " + n) for k, v, n in SDWAN_SENDS]

EXPECT_CURRENT_PROBLEMS = 1
EXPECT_WEB01_PROBLEM_EVENTS = 3
EXPECT_DB01_PROBLEM_EVENTS = 1
EXPECT_SDWAN_BW_IN_MAX = 8192
EXPECT_SDWAN_BW_OUT_MAX = 3072
EXPECT_SDWAN_MEM = 1073741824


def rpc(method, params, auth=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if auth:
        body["auth"] = auth
    res = httpx.post(ZABBIX_API, json=body, timeout=60).json()
    if "error" in res:
        raise RuntimeError("%s -> %s" % (method, res["error"]))
    return res.get("result")


def login():
    return rpc("user.login", {"username": ZABBIX_USER, "password": ZABBIX_PASSWORD})


def find_host(sid, name):
    res = rpc("host.get", {"output": ["hostid", "name"], "filter": {"host": name}}, auth=sid)
    return res[0] if res else None


def zabbix_send(host, key, value, server=TRAPPER_HOST, port=TRAPPER_PORT, timeout=10):
    """直接实现 Zabbix sender 协议，避免依赖 zabbix_sender 命令。

    协议：'ZBXD\\x01' + 8 字节小端长度 + JSON 负载。
    """
    payload = json.dumps(
        {"request": "sender data", "data": [{"host": host, "key": key, "value": str(value)}]}
    ).encode("utf-8")
    packet = b"ZBXD\x01" + struct.pack("<Q", len(payload)) + payload

    with socket.create_connection((server, port), timeout=timeout) as sock:
        sock.sendall(packet)
        header = b""
        while len(header) < 13:
            chunk = sock.recv(13 - len(header))
            if not chunk:
                raise RuntimeError("连接被对端关闭")
            header += chunk
        if not header.startswith(b"ZBXD"):
            raise RuntimeError("响应头异常: %r" % header[:4])
        length = struct.unpack("<Q", header[5:13])[0]
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                break
            body += chunk
    return json.loads(body.decode("utf-8"))


def send_values(verbose=True):
    """投递全部取值，返回被拒收的条数。

    必须检查返回里的 failed：Zabbix 对无法投递的值会明确回 `processed: 0; failed: 1`
    （最常见原因见 wait_for_config_cache）。旧实现在这种情况下只打印不报错，
    于是产出一份"看着正常、其实少了几次跳变"的假数据，自检却对不上——比失败更危险。
    """
    failed = 0
    for host, key, val, note in SEND_PLAN:
        try:
            info = zabbix_send(host, key, val).get("info", "")
        except Exception as exc:
            info = "ERROR: %r" % (exc,)
        if "failed: 0" not in info:
            failed += 1
            info = "[拒收!] " + info
        if verbose:
            print("    send %-14s %-16s = %-6s  %-30s  %s" % (host, key, val, note, info))
        time.sleep(1.5)  # 让触发器逐个求值，避免同一秒内被合并
    return failed


def wait_for_config_cache(sid, timeout=180):
    """等 Zabbix server 的配置缓存认识新建的监控项。

    实测（决定性实验）：新建 trapper 监控项后**立刻**发送，返回
    `processed: 0; failed: 1`，值被直接丢弃、历史表里什么都没有；等配置缓存刷新
    （CacheUpdateFrequency，默认 60s）后再发才变成 `processed: 1; failed: 0`。

    缓存是按固定周期整点刷新的，所以"要等多久"是 0~60s 的随机值——fixture 因此
    有时会丢掉告警序列的前几次跳变。这里用探针值（低于触发器阈值，不会产生告警）
    反复试，直到真的被接收为止。
    """
    probes = [(PREFIX + "WEB01", "aiqa.cpu"), (PREFIX + "DB01", "aiqa.disk.used")]
    started = time.time()
    while time.time() - started < timeout:
        ready = True
        for host, key in probes:
            try:
                info = zabbix_send(host, key, 0).get("info", "")
            except Exception as exc:
                info = "ERROR: %r" % (exc,)
            if "failed: 0" not in info:
                ready = False
                print("    [等待配置缓存] %s / %s -> %s" % (host, key, info))
        if ready:
            print("  等待 %.0fs 后，探针值已被接收" % (time.time() - started))
            return True
        time.sleep(10)
    return False


def fixture_hostids(sid):
    return [
        h["hostid"]
        for h in rpc("host.get", {"output": ["hostid"], "search": {"name": PREFIX}}, auth=sid)
    ]


def problems_for(sid):
    ids = fixture_hostids(sid)
    if not ids:
        return []
    return rpc("problem.get", {"output": ["eventid", "name", "severity"], "hostids": ids}, auth=sid)


def event_counts_today(sid):
    hosts = rpc("host.get", {"output": ["hostid", "name"], "search": {"name": PREFIX}}, auth=sid)
    by_id = {h["hostid"]: h["name"] for h in hosts}
    if not hosts:
        return {}
    start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    events = rpc(
        "event.get",
        {
            "output": ["eventid"],
            "hostids": list(by_id),
            "value": 1,
            "time_from": start,
            "time_till": int(time.time()),
            "selectHosts": ["hostid"],
            "limit": 500,
        },
        auth=sid,
    )
    counts = {}
    for ev in events:
        for h in ev.get("hosts") or []:
            name = by_id.get(h["hostid"], h["hostid"])
            counts[name] = counts.get(name, 0) + 1
    return counts


def do_setup():
    sid = login()
    print("=== 1) 主机组 ===")
    grp = rpc("hostgroup.get", {"output": ["groupid"], "filter": {"name": GROUP_NAME}}, auth=sid)
    if grp:
        gid = grp[0]["groupid"]
        print("  已存在 %s (groupid=%s)" % (GROUP_NAME, gid))
    else:
        gid = rpc("hostgroup.create", {"name": GROUP_NAME}, auth=sid)["groupids"][0]
        print("  已创建 %s (groupid=%s)" % (GROUP_NAME, gid))

    print("\n=== 2) 主机与监控项 ===")
    for spec in HOSTS:
        existing = find_host(sid, spec["host"])
        if existing:
            hid = existing["hostid"]
            print("  [跳过] %-14s 已存在 (hostid=%s)" % (spec["host"], hid))
        else:
            params = {"host": spec["host"], "groups": [{"groupid": gid}]}
            if spec["ip"]:
                iface = {
                    "type": spec.get("if_type", 1),
                    "main": 1,
                    "useip": 1,
                    "ip": spec["ip"],
                    "dns": "",
                    "port": spec.get("port", "10050"),
                }
                if spec.get("if_type") == 2:
                    # SNMP 接口必须给 details，否则 host.create 直接报
                    # "Invalid params. Incorrect arguments passed to function."
                    # （已实测：不带 details 必失败，且报错信息不指出缺哪个字段）
                    iface["details"] = {"version": 2, "bulk": 1, "community": "public"}
                params["interfaces"] = [iface]
            hid = rpc("host.create", params, auth=sid)["hostids"][0]
            print("  [创建] %-14s hostid=%-6s ip=%-14s if_type=%s"
                  % (spec["host"], hid, spec["ip"] or "(无接口)", spec.get("if_type", 1)))

        have_keys = {i["key_"] for i in rpc("item.get", {"output": ["key_"], "hostids": [hid]}, auth=sid)}
        for label, key, units in spec["items"]:
            if key in have_keys:
                continue
            rpc(
                "item.create",
                {"hostid": hid, "name": label, "key_": key, "type": 2, "value_type": 0,
                 "delay": "0", "units": units},
                auth=sid,
            )
            print("      + 监控项 %s (%s)" % (key, units))

    print("\n=== 2b) SNMP 主机的特殊监控项（文本项 / 禁用项）===")
    sdwan = find_host(sid, SDWAN_HOST)
    if sdwan is None:
        print("  [警告] 未找到 %s，跳过" % SDWAN_HOST)
    else:
        shid = sdwan["hostid"]
        have = {
            i["key_"]: i
            for i in rpc("item.get", {"output": ["itemid", "key_"], "hostids": [shid]}, auth=sid)
        }
        if SDWAN_TEXT_KEY in have:
            print("  [跳过] 文本项 %s" % SDWAN_TEXT_KEY)
        else:
            rpc(
                "item.create",
                {"hostid": shid, "name": "Network interfaces discovery",
                 "key_": SDWAN_TEXT_KEY, "type": 2, "value_type": 4, "delay": "0"},
                auth=sid,
            )
            print("  [创建] 文本项 %s (value_type=4)" % SDWAN_TEXT_KEY)
        if SDWAN_DISABLED_KEY in have:
            print("  [跳过] 禁用项 %s" % SDWAN_DISABLED_KEY)
        else:
            did = rpc(
                "item.create",
                {"hostid": shid, "name": "Interface eth9(): Bits received",
                 "key_": SDWAN_DISABLED_KEY, "type": 2, "value_type": 0,
                 "delay": "0", "units": "bps"},
                auth=sid,
            )["itemids"][0]
            rpc("item.update", {"itemid": did, "status": 1}, auth=sid)
            print("  [创建] 禁用项 %s (status=1，不会有数据)" % SDWAN_DISABLED_KEY)

    print("\n=== 3) 触发器 ===")
    want_desc = {desc for desc, _e, _p in TRIGGERS}
    fhosts = [
        h["hostid"]
        for h in rpc("host.get", {"output": ["hostid"], "search": {"host": PREFIX}}, auth=sid)
    ]
    existing = (
        rpc("trigger.get", {"output": ["triggerid", "description"], "hostids": fhosts}, auth=sid)
        if fhosts
        else []
    )
    # 历史遗留的重复触发器会把"未恢复告警数"翻倍，让 fixture 自检永远对不上
    # （实测踩过：上一轮会话留下的同名触发器多报了 1 条未恢复、事件数翻倍）。
    # 因此把 fixture 主机上不属于当前定义的触发器清掉——只动 PREFIX 开头的测试主机。
    stale = [t["triggerid"] for t in existing if t["description"] not in want_desc]
    if stale:
        rpc("trigger.delete", stale, auth=sid)
        print("  [清理] 删除历史遗留触发器 %s" % stale)
    have_desc = {t["description"] for t in existing if t["triggerid"] not in stale}
    for desc, expr, prio in TRIGGERS:
        if desc in have_desc:
            print("  [跳过] %s" % desc)
            continue
        rpc("trigger.create", {"description": desc, "expression": expr, "priority": prio}, auth=sid)
        print("  [创建] %-26s %s (severity=%d)" % (desc, expr, prio))

    print("\n=== 3b) 等待 Zabbix 配置缓存认识新建的监控项 ===")
    if wait_for_config_cache(sid):
        print("  [OK] trapper 探针已被接收，可以开始制造告警序列")
    else:
        print("  [警告] 探针一直被拒收，后续告警序列很可能不完整")

    print("\n=== 4) 制造告警序列 ===")
    send_failed = send_values()

    print("\n=== 5) 等待触发器求值 ===")
    web, db = PREFIX + "WEB01", PREFIX + "DB01"
    converged = False
    for i in range(1, 21):
        time.sleep(5)
        probs = problems_for(sid)
        counts = event_counts_today(sid)
        print("  第 %2d 次轮询(%3ds): 未恢复 %d 条; 今日事件 %s"
              % (i, i * 5, len(probs), json.dumps(counts, ensure_ascii=False)))
        # 必须同时等"未恢复条数"与"事件计数"：只等前者会在第一条告警出现时就提前收敛，
        # 后面几次事件还没落库就被判成"与预期不符"（实测踩过）。
        if (len(probs) == EXPECT_CURRENT_PROBLEMS
                and counts.get(web) == EXPECT_WEB01_PROBLEM_EVENTS
                and counts.get(db) == EXPECT_DB01_PROBLEM_EVENTS):
            converged = True
            break
    if not converged:
        print("  [警告] 未在 100s 内收敛到预期")

    probs = problems_for(sid)
    counts = event_counts_today(sid)
    print("\n=== 6) 收敛结果 ===")
    for p in probs:
        print("  未恢复: %s (severity=%s)" % (p["name"], p["severity"]))
    print("  今日问题事件数: %s" % json.dumps(counts, ensure_ascii=False))
    print("  被拒收的发送次数: %d%s"
          % (send_failed, "（fixture 数据不可用！）" if send_failed else ""))

    ok = (
        send_failed == 0
        and len(probs) == EXPECT_CURRENT_PROBLEMS
        and counts.get(PREFIX + "WEB01") == EXPECT_WEB01_PROBLEM_EVENTS
        and counts.get(PREFIX + "DB01") == EXPECT_DB01_PROBLEM_EVENTS
    )
    print("\n  ==> fixture %s" % ("符合预期" if ok else "与预期不符，请检查触发器/发送链路"))
    rpc("user.logout", [], sid)
    return 0 if ok else 1


def do_status():
    sid = login()
    hosts = rpc("host.get", {"output": ["hostid", "name"], "search": {"name": PREFIX},
                             "selectInterfaces": ["ip", "port"]}, auth=sid)
    print("=== fixture 主机 ===")
    for h in hosts:
        ifs = h.get("interfaces") or []
        print("  %-14s hostid=%-6s ip=%s" % (h["name"], h["hostid"], ifs[0]["ip"] if ifs else "(无接口)"))
    print("\n=== 当前未恢复问题 ===")
    for p in problems_for(sid):
        print("  %s (severity=%s)" % (p["name"], p["severity"]))
    print("\n=== 今日问题事件数 ===")
    print("  %s" % json.dumps(event_counts_today(sid), ensure_ascii=False))
    print("\n=== 各监控项最新值 ===")
    for it in rpc("item.get", {"output": ["key_", "lastvalue"], "search": {"key_": "aiqa."}}, auth=sid):
        print("  %-16s = %s" % (it["key_"], it.get("lastvalue")))

    print("\n=== SNMP 主机监控项（含禁用项）===")
    print("  期望: 入向峰值=%s  出向峰值=%s  内存=%s"
          % (EXPECT_SDWAN_BW_IN_MAX, EXPECT_SDWAN_BW_OUT_MAX, EXPECT_SDWAN_MEM))
    sdwan = find_host(sid, SDWAN_HOST)
    if sdwan:
        for it in rpc(
            "item.get",
            {"output": ["key_", "name", "status", "value_type", "lastvalue"],
             "hostids": [sdwan["hostid"]]},
            auth=sid,
        ):
            print("  %-32s vt=%s status=%s last=%s"
                  % (it["key_"], it["value_type"], it["status"], it.get("lastvalue")))
    rpc("user.logout", [], sid)
    return 0


def do_teardown():
    sid = login()
    hosts = rpc("host.get", {"output": ["hostid", "name"], "search": {"name": PREFIX}}, auth=sid)
    if hosts:
        rpc("host.delete", [h["hostid"] for h in hosts], auth=sid)
        print("已删除主机: %s" % [h["name"] for h in hosts])
    else:
        print("没有需要删除的主机")
    grp = rpc("hostgroup.get", {"output": ["groupid"], "filter": {"name": GROUP_NAME}}, auth=sid)
    if grp:
        try:
            rpc("hostgroup.delete", [grp[0]["groupid"]], auth=sid)
            print("已删除主机组: %s" % GROUP_NAME)
        except RuntimeError as exc:
            print("主机组删除失败（可能仍被引用）: %s" % exc)
    left = rpc("host.get", {"output": ["hostid"], "search": {"name": PREFIX}}, auth=sid)
    print("残留 fixture 主机: %d" % len(left))
    rpc("user.logout", [], sid)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "setup":
        sys.exit(do_setup())
    if mode == "status":
        sys.exit(do_status())
    if mode == "teardown":
        sys.exit(do_teardown())
    print("用法: qa_fixture.py setup|status|teardown")
    sys.exit(2)
