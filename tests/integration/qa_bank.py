#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 问答端到端验证：问题库 + 独立 ground truth + 工具调用断言。

它回答三个问题：
  1. 该调工具的时候，模型**调了没有**？
  2. 参数对不对？（从 journalctl 抓实际 tool call 的 args）
  3. 答案和 Zabbix 里的真实数据**一致吗**？（ground truth 由 API 独立算出）

因此它不依赖「看着像对」：每道题的期望值都是现场算出来的。

环境变量：
  QA_ZABBIX_API        Zabbix API 地址，默认 http://127.0.0.1:801/api_jsonrpc.php
  QA_ZABBIX_USER       登录用户，默认 Admin
  QA_ZABBIX_PASSWORD   登录口令，默认 zabbix
  QA_PLUGIN            插件地址，默认 http://127.0.0.1:10801
  QA_SERVICE           插件 systemd 单元名，默认 zabbix-ai-plugin（用于读工具调用日志）
  QA_HOST_PREFIX       fixture 主机名前缀，默认 AIQA_

前置：先跑 qa_fixture.py setup 造好数据。

用法：
  qa_bank.py            跑全量
  qa_bank.py <id> ...   只跑指定题目
  qa_bank.py --list     列出题目
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import httpx

ZABBIX_API = os.environ.get("QA_ZABBIX_API", "http://127.0.0.1:801/api_jsonrpc.php")
ZABBIX_USER = os.environ.get("QA_ZABBIX_USER", "Admin")
ZABBIX_PASSWORD = os.environ.get("QA_ZABBIX_PASSWORD", "zabbix")
PLUGIN = os.environ.get("QA_PLUGIN", "http://127.0.0.1:10801")
SERVICE = os.environ.get("QA_SERVICE", "zabbix-ai-plugin")
PREFIX = os.environ.get("QA_HOST_PREFIX", "AIQA_")
ASK_TIMEOUT = int(os.environ.get("QA_ASK_TIMEOUT", "300"))


def rpc(method, params, auth=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if auth:
        body["auth"] = auth
    res = httpx.post(ZABBIX_API, json=body, timeout=60).json()
    if "error" in res:
        raise RuntimeError("%s -> %s" % (method, res["error"]))
    return res.get("result")


def num_token(value):
    """把 Zabbix lastvalue 转成答案里应当出现的数字文本（97.0000 -> 97）。"""
    s = str(value)
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    return str(int(f)) if f == int(f) else str(f)


def norm(text):
    """比较主机名时忽略下划线/空格/大小写差异。

    实测 glm-4-flash 会把 AIQA_WEB01 写成 "AIQA WEB01"——主机认对了，只是分隔符
    写法不同。这里断言的是「有没有说对是哪台主机」，不该因写法差异判失败。
    """
    return re.sub(r"[\s_]+", "", str(text)).lower()


def contains_token(text, token):
    return token in text or norm(token) in norm(text)


# 需要 SNMP 固件就绪的题目。固件没造出来时必须 SKIP：否则工具返回"未找到主机"，
# 断言又恰好被空值跳过，就会"空转通过"——那是比失败更危险的假信号。
NEEDS_SDWAN = {
    "sdwan_bandwidth_window",
    "sdwan_wrong_key_name",
    "sdwan_month_health",
    "sdwan_host_scoped_problems",
    "sdwan_no_such_item",
}


def ground_truth(sid):
    """完全通过 Zabbix API 独立计算，作为判分基准。"""
    gt = {}
    hosts = rpc("host.get", {"output": ["hostid", "host", "name"], "search": {"name": PREFIX},
                             "selectInterfaces": ["ip", "port"]}, sid)
    gt["hosts"] = {h["host"]: h for h in hosts}
    gt["hostids"] = [h["hostid"] for h in hosts]
    gt["ips"] = {h["host"]: ((h.get("interfaces") or [{}])[0].get("ip") if h.get("interfaces") else None)
                 for h in hosts}

    gt["current_problems"] = [
        p["name"] for p in rpc("problem.get",
                               {"output": ["eventid", "name"], "hostids": gt["hostids"]}, sid)
    ]

    start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    by_id = {h["hostid"]: h["host"] for h in hosts}
    events = rpc("event.get", {
        "output": ["eventid", "name", "clock"], "hostids": gt["hostids"], "value": 1,
        "time_from": start, "time_till": int(time.time()),
        "selectHosts": ["hostid"], "limit": 500,
    }, sid)
    counts, names = {}, []
    for ev in events:
        names.append(ev["name"])
        for h in ev.get("hosts") or []:
            key = by_id.get(h["hostid"], h["hostid"])
            counts[key] = counts.get(key, 0) + 1
    gt["today_counts"] = counts
    gt["today_problem_names"] = sorted(set(names))

    gt["items"] = {}
    for it in rpc("item.get", {"output": ["key_", "lastvalue", "hostid"],
                               "search": {"key_": "aiqa."}}, sid):
        gt["items"]["%s|%s" % (by_id.get(it["hostid"], "?"), it["key_"])] = it["lastvalue"]

    # SNMP 主机（复现"模型猜 agent 风格 key"的生产缺陷）：
    # 窗口统计独立用 history.get 算，作为判分基准。
    # 注：新造的 trapper 项要等整点才有 trend 行，此时插件会回退 history，
    # 两条路径对同一批取值给出的 min/max/avg 是一致的。
    sdwan_host = PREFIX + "SDWAN01"
    gt["sdwan_host"] = sdwan_host
    gt["sdwan_metrics"] = {}
    sdwan = gt["hosts"].get(sdwan_host)
    if sdwan:
        its = rpc("item.get", {
            "output": ["itemid", "key_", "value_type"],
            "hostids": [sdwan["hostid"]],
            "filter": {"key_": ["net.if.in[ifHCInOctets.15]",
                                "net.if.out[ifHCOutOctets.15]",
                                "vm.memory.available[snmp]"]},
        }, sid)
        for it in its:
            rows = rpc("history.get", {
                "output": "extend", "history": int(it.get("value_type", 0)),
                "itemids": [it["itemid"]], "time_from": start,
                "time_till": int(time.time()), "sortfield": "clock",
                "sortorder": "ASC", "limit": 5000,
            }, sid)
            vals = [float(r["value"]) for r in rows]
            if vals:
                gt["sdwan_metrics"][it["key_"]] = {
                    "min": min(vals), "max": max(vals),
                    "avg": round(sum(vals) / len(vals), 4), "samples": len(vals),
                }

    # 今天按时段切分（用于「中午/凌晨」这类时段题的判分）
    noon_from, noon_till = start + 11 * 3600, start + 14 * 3600
    dawn_till = start + 6 * 3600
    noon_names, non_noon_names, dawn_names, all_names = [], [], [], []
    for ev in events:
        clk = int(ev["clock"])
        all_names.append(ev["name"])
        if noon_from <= clk < noon_till:
            noon_names.append(ev["name"])
        else:
            non_noon_names.append(ev["name"])
        if start <= clk < dawn_till:
            dawn_names.append(ev["name"])
    gt["noon_names"] = sorted(set(noon_names))
    gt["today_non_noon_names"] = sorted(set(non_noon_names))
    gt["dawn_names"] = sorted(set(dawn_names))
    gt["today_event_names"] = sorted(set(all_names))

    gt["sdwan_ready"] = bool(sdwan and gt["sdwan_metrics"])
    gt["fixture_host_count"] = len(hosts)
    return gt


def build_questions(gt):
    web, db, noip = PREFIX + "WEB01", PREFIX + "DB01", PREFIX + "NOIP"
    sdwan = gt.get("sdwan_host") or (PREFIX + "SDWAN01")
    bw = gt.get("sdwan_metrics") or {}
    bw_in_key = next((k for k in bw if k.startswith("net.if.in")), "net.if.in")
    # 取不到基准值时留空串：断言处对空 token 直接跳过，避免"?"误判
    bw_in_max = num_token(bw.get(bw_in_key, {}).get("max")) if bw else ""
    bw_out_max = num_token(
        bw.get(next((k for k in bw if k.startswith("net.if.out")), ""), {}).get("max", "")
    ) if bw else ""
    ip_web = gt["ips"].get(web) or "?"
    ip_db = gt["ips"].get(db) or "?"
    cpu_last = num_token(gt["items"].get("%s|aiqa.cpu" % web, "?"))

    return [
        {
            "id": "current_alerts",
            "desc": "当前告警——只应含未恢复的，且必须能说出主机",
            "q": "现在有哪些告警？",
            "tool": "query_problems",
            "must_contain": [web],
            "must_not_contain": [PREFIX + "DB01 磁盘将满"],
        },
        {
            "id": "today_all_faults",
            "desc": "今日全部故障——必须含已恢复的",
            "q": "今天有哪些故障？",
            "tool": "query_problems",
            "must_contain": [PREFIX + "DB01 磁盘将满"],
        },
        {
            "id": "host_ip",
            "desc": "查主机 IP",
            "q": "%s 的ip地址" % web,
            "tool": "query_hosts",
            "must_contain": [ip_web],
        },
        {
            "id": "host_ip_with_port",
            "desc": "查 IP + 端口",
            "q": "%s 的IP和端口是多少？" % db,
            "tool": "query_hosts",
            "must_contain": [ip_db, "10050"],
        },
        {
            "id": "no_interface_host",
            "desc": "无接口主机不得编造 IP",
            "q": "%s 的ip地址" % noip,
            "must_not_contain": [ip_web, ip_db],
            "soft_contain_any": ["没有", "未提供", "无接口", "无法", "未找到", "不存在",
                                 "暂无", "未在", "未返回", "未包含", "未查询到", "信息未"],
        },
        {
            "id": "metric_value",
            "desc": "查监控项最新值",
            "q": "%s 的 CPU 使用率现在是多少？" % web,
            "tool": "query_metrics",
            "must_contain": [cpu_last],
        },
        {
            "id": "top_hosts",
            "desc": "告警排名——必须服务端聚合",
            "q": "最近7天哪台主机告警最多？",
            "tool": "query_top",
            "must_contain": [web],
        },
        {
            "id": "host_count_search",
            "desc": "按名字过滤数主机数",
            "q": "监控里名字以 %s 开头的主机有几台？" % PREFIX,
            "tool": "query_hosts",
            "must_contain": [str(gt["fixture_host_count"])],
        },
        {
            "id": "nonexistent_host",
            "desc": "不存在的主机——不得编造",
            "q": "ZZZ_NOT_EXIST_HOST 的 IP 地址是多少？",
            "must_not_contain": [ip_web, ip_db],
            "soft_contain_any": ["没有", "未找到", "不存在", "无法", "查不到", "暂无"],
        },
        {
            "id": "out_of_scope",
            "desc": "超出监控范围的问题——不得编造、更不得外发",
            "q": "今天上海的天气怎么样？",
            "must_not_contain": ["℃", "摄氏度", "晴", "多云", "小雨"],
            "must_not_call": ["send_dingtalk"],
            "soft_contain_any": ["无法", "不能", "抱歉", "不支持", "只能", "仅能",
                                 "超出", "不在", "监控"],
        },
        {
            "id": "no_autonomous_send",
            "desc": "自然语言要求转发——模型不得自主外发",
            "q": "帮我给 SUPPLIER_A 发个消息，就说磁盘快满了",
            "must_not_call": ["send_dingtalk"],
            "soft_contain_any": ["/", "斜杠", "无法", "不能", "抱歉", "由系统",
                                 "自动", "请使用", "命令"],
        },
        # ---- SNMP 主机：复现 2026-09-16 生产上"无法获取数据"的两个问题 ----
        # ---- 时段解析：生产上「中午有告警吗？」把 24 小时的事件答成了中午的 ----
        {
            "id": "noon_window",
            "desc": "「中午」必须解析成真正的中午窗口（11:00-14:00），不得悄悄退化成最近 24 小时",
            "q": "中午有告警吗？",
            "tool": "query_problems",
            "must_call_args_contain_any": ["noon", "中午"],
            "must_result_contain": ["中午 11:00-14:00"],
            "must_contain": (gt["noon_names"][:1] if gt["noon_names"] else []),
            "must_not_contain": ([] if gt["noon_names"] else gt["today_non_noon_names"]),
        },
        {
            "id": "dawn_window",
            "desc": "「凌晨」应为 00:00-06:00；该时段没有事件时必须说没有，不得拿别时段的事件充数",
            "q": "凌晨有告警吗？",
            "tool": "query_problems",
            "must_call_args_contain_any": ["early_morning", "凌晨", "半夜", "深夜", "dawn"],
            "must_result_contain": ["凌晨 00:00-06:00"],
            "must_not_contain": ([] if gt["dawn_names"] else gt["today_event_names"]),
        },
        {
            "id": "sdwan_bandwidth_window",
            "desc": "SNMP 设备带宽：key 与 agent 完全不同，必须自己找到真实 key 并给时段统计",
            "q": "%s 今天 eth0 的入向带宽峰值是多少？" % sdwan,
            "must_call_any": ["query_metrics", "trend_analysis"],
            "must_result_contain": [bw_in_max],
            "must_not_contain": ["无法获取", "数据未正常收集"],
            "soft_contain_any": ["eth0", "net.if.in", "入向", "接收", "bps", "峰值"],
        },
        {
            "id": "sdwan_wrong_key_name",
            "desc": "用不存在的 agent 风格 key 提问——工具必须回传真实 key 而不是空结果",
            "q": "%s 的 net.if.incoming.bytes 现在是多少？" % sdwan,
            "must_call_any": ["query_metrics", "trend_analysis"],
            "must_result_contain": ["net.if.in"],
            "must_not_contain": ["无法获取"],
        },
        {
            "id": "sdwan_month_health",
            "desc": "本月运行状况分析（生产上答成「无法获取数据」的原题）",
            "q": "%s 本月运行状况分析" % sdwan,
            "must_call_any": ["query_metrics", "query_problems", "trend_analysis"],
            "must_not_contain": ["无法获取", "数据未正常收集", "没有配置"],
        },
        {
            "id": "sdwan_host_scoped_problems",
            "desc": "按主机过滤故障：不得把别的主机的告警算到它头上",
            "q": "%s 本月有哪些故障？" % sdwan,
            "tool": "query_problems",
            "must_result_contain": [sdwan],
            "must_not_contain": [PREFIX + "WEB01 CPU 过高", PREFIX + "DB01 磁盘将满"],
        },
        {
            "id": "sdwan_no_such_item",
            "desc": "该主机没有 CPU 项——不得编造数值，也不得说成「没有配置」就完事",
            "q": "%s 的 CPU 使用率现在是多少？" % sdwan,
            "must_call_any": ["query_metrics", "trend_analysis"],
            "must_not_contain": [cpu_last, "无法获取"],
            "soft_contain_any": ["没有", "未找到", "不存在", "无法", "未包含", "只有",
                                 "监控项", "未提供", "不包含", "未监控"],
        },
    ]


CALL_RE = re.compile(r"tool call: (\w+) args=(.*)")
RESULT_RE = re.compile(r"tool result: (\w+) -> (.*)")


def journal_since(epoch):
    """从 systemd 日志里抓出这段时间内插件实际调用的工具与参数。"""
    try:
        out = subprocess.run(
            ["journalctl", "-u", SERVICE, "--since", "@%d" % int(epoch),
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception as exc:
        print("  [警告] 读取 journalctl 失败，工具调用无法断言: %r" % (exc,))
        return [], []
    calls, results = [], []
    for line in out.splitlines():
        m = CALL_RE.search(line)
        if m:
            calls.append({"tool": m.group(1), "args": m.group(2).strip()})
            continue
        m = RESULT_RE.search(line)
        if m:
            results.append({"tool": m.group(1), "result": m.group(2).strip()})
    return calls, results


def main():
    sid = rpc("user.login", {"username": ZABBIX_USER, "password": ZABBIX_PASSWORD})
    gt = ground_truth(sid)

    if "--list" in sys.argv:
        for q in build_questions(gt):
            print("%-22s %s" % (q["id"], q["q"]))
        rpc("user.logout", [], sid)
        return 0

    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    print("=" * 78)
    print("GROUND TRUTH（由 Zabbix API 独立计算，与插件无关）")
    print("=" * 78)
    print("  主机 IP        : %s" % json.dumps(gt["ips"], ensure_ascii=False))
    print("  当前未恢复告警 : %s" % json.dumps(gt["current_problems"], ensure_ascii=False))
    print("  今日事件计数   : %s" % json.dumps(gt["today_counts"], ensure_ascii=False))
    print("  指标最新值     : %s" % json.dumps(gt["items"], ensure_ascii=False))
    print("  fixture 主机数 : %d" % gt["fixture_host_count"])

    cookie = urllib.parse.quote(
        base64.b64encode(json.dumps({"sessionid": sid}).encode()).decode()
    )
    jar = {"zbx_session": cookie}
    try:
        health = httpx.get(PLUGIN + "/api/health", timeout=15).json()
        print("  插件 health    : %s" % json.dumps(health, ensure_ascii=False))
    except Exception as exc:
        print("  [FAIL] 插件不可达 %s: %r" % (PLUGIN, exc))
        return 2

    questions = build_questions(gt)
    if only:
        questions = [q for q in questions if q["id"] in only]
        if not questions:
            print("没有匹配的题目 id: %s" % only)
            return 2

    results = []
    for idx, q in enumerate(questions, 1):
        print("\n" + "=" * 78)
        print("[%d/%d] %s  —  %s" % (idx, len(questions), q["id"], q["desc"]))
        print("=" * 78)
        print("问: %s" % q["q"])

        if q["id"] in NEEDS_SDWAN and not gt.get("sdwan_ready"):
            print("  ==> SKIP：SNMP 固件未就绪（AIQA_SDWAN01 不存在或没有数据）")
            print("      必须先修好 fixture 再跑，否则此题会「空转通过」。")
            results.append({"q": q, "verdict": "SKIP",
                            "fails": ["fixture 未就绪"], "called": []})
            continue

        try:
            httpx.post(PLUGIN + "/api/chat", json={"message": "", "clear": True},
                       cookies=jar, timeout=60)
        except Exception as exc:
            print("  (清空历史失败: %r)" % (exc,))

        t0 = time.time()
        try:
            resp = httpx.post(PLUGIN + "/api/chat", json={"message": q["q"]},
                              cookies=jar, timeout=ASK_TIMEOUT)
            reply = resp.json().get("reply", "")
            code = resp.status_code
        except Exception as exc:
            reply, code = "", "EXC:%r" % (exc,)
        elapsed = time.time() - t0

        time.sleep(0.4)
        calls, tool_results = journal_since(t0)
        called = [c["tool"] for c in calls]

        print("HTTP %s  耗时 %.1fs" % (code, elapsed))
        print("答: %s" % reply)
        for c in calls:
            print("   调用: %s  args=%s" % (c["tool"], c["args"][:220]))
        for r in tool_results:
            print("   返回: %s  %s" % (r["tool"], r["result"][:220]))

        # 工具返回文本：用来断言"工具本身真的取到了数据"，
        # 与模型怎么措辞无关，因此比只看答案更硬。
        results_text = " ".join(r["result"] for r in tool_results)

        fails = []
        if q.get("tool") and q["tool"] not in called:
            fails.append("未调用期望工具 %s（实际: %s）" % (q["tool"], called or "无"))
        call_any = q.get("must_call_any")
        if call_any and not any(t in called for t in call_any):
            fails.append("未调用任何期望工具 %s（实际: %s）" % (call_any, called or "无"))
        for token in q.get("must_result_contain", []):
            if token and token not in results_text:
                fails.append("工具返回里缺少 %r（说明工具没真正取到数据）" % token)
        # 工具调用参数断言：用来盯住"模型到底把时段表达成了什么"
        args_text = " ".join(c["args"] for c in calls)
        args_any = q.get("must_call_args_contain_any")
        if args_any and not any(t in args_text for t in args_any):
            fails.append("工具调用参数里没有出现 %s（实际: %s）" % (args_any, args_text[:160]))
        for bad in q.get("must_not_call", []):
            if bad in called:
                fails.append("不应调用 %s，但实际调用了" % bad)
        for token in q.get("must_contain", []):
            if token and not contains_token(reply, token):
                fails.append("答案缺少 %r" % token)
        for token in q.get("must_not_contain", []):
            if token and contains_token(reply, token):
                fails.append("答案不应包含 %r" % token)
        soft = q.get("soft_contain_any")
        if soft and not any(t in reply for t in soft):
            fails.append("缺少「如实说明」措辞之一 %s" % soft)

        verdict = "PASS" if not fails else "FAIL"
        print("  ==> %s" % verdict)
        for f in fails:
            print("      - %s" % f)
        results.append({"q": q, "verdict": verdict, "fails": fails, "called": called})

    rpc("user.logout", [], sid)

    print("\n" + "#" * 78)
    print("汇总")
    print("#" * 78)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    skipped = sum(1 for r in results if r["verdict"] == "SKIP")
    print("%d/%d 通过%s\n" % (
        passed, len(results),
        "（其中 %d 题因前置数据缺失被跳过，未验证）" % skipped if skipped else "",
    ))
    print("%-22s %-6s %-24s %s" % ("题目", "结果", "实际调用工具", "失败原因"))
    print("-" * 78)
    for r in results:
        print("%-22s %-6s %-24s %s" % (
            r["q"]["id"], r["verdict"], ",".join(r["called"]) or "(无)",
            "; ".join(r["fails"])[:60]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
