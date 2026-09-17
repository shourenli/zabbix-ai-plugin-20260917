"""工具层：供 LLM 调用的五个工具。

- query_problems   : 当前/历史问题，可按主机与时间范围过滤
- query_top        : 某时段告警最多的主机/触发器 TOP N（服务端聚合）
- query_metrics    : 某主机监控项的最近值 / 时间段统计（key 未命中时回传真实 key 清单）
- query_hosts      : 主机清单与运行状况概览（含 ip/port/接口类型）
- trend_analysis   : 读 trend 数据算增速并外推预测（如"磁盘几天满"）

工具返回结构化 JSON，由 LLM 翻译成自然语言。

send_dingtalk **不在**这里注册给 LLM（原因见 build_tool_schemas 末尾）：发送消息有真实
外部副作用，只能由用户在输入框以「/供应商名 消息内容」触发。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .dingtalk import DingtalkError, send_text
from .llm import Tool
from .zabbix import ZabbixClient, ZabbixError

if TYPE_CHECKING:  # 仅类型标注用，避免运行时循环依赖
    from .config import SupplierTarget

logger = logging.getLogger("zabbix_ai.tools")

SEVERITY = ["未分类", "信息", "警告", "一般严重", "严重", "灾难"]

# 钉钉消息模板默认值（可被 config.yaml 的 dingtalk.message_template 或
# 供应商级的 message_template 覆盖）。占位符：{username} / {message}
DEFAULT_MESSAGE_TEMPLATE = "[zabbix-AI] {username} 上报：\n{message}"

_TEMPLATE_PLACEHOLDER = re.compile(r"\{(username|message)\}")


def render_message(template: str, username: str, message: str) -> str:
    """按模板渲染钉钉消息正文。

    单次正则替换：避免 str.format 对正文里 {} 的干扰，也避免二次替换
    （例如用户名里恰好含 "{message}"）。模板中未知的占位符原样保留。
    """
    values = {"username": username, "message": message}
    return _TEMPLATE_PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


class ToolContext:
    """持有会话、zabbix 客户端、供应商表、审计回调等运行期上下文。"""

    def __init__(
        self,
        sessionid: str,
        username: str,
        zclient: ZabbixClient,
        suppliers: Dict[str, "SupplierTarget"],
        audit: Callable[[str, str, str, str], None],  # (username, action, target, detail)
        message_template: str = DEFAULT_MESSAGE_TEMPLATE,
    ):
        self.sessionid = sessionid
        self.username = username
        self.zclient = zclient
        self.suppliers = suppliers
        self.audit = audit
        self.message_template = message_template or DEFAULT_MESSAGE_TEMPLATE


def _now_ts() -> int:
    return int(time.time())


def _days_ago(days: int) -> int:
    return _now_ts() - days * 86400


# ---- 时间段解析（由服务端按本机时区计算，避免让模型做时间换算）----
PERIODS = {
    "today": "今天 00:00 至今",
    "early_morning": "今天凌晨 00:00-06:00",
    "morning": "今天上午 00:00-12:00",
    "noon": "今天中午 11:00-14:00",
    "afternoon": "今天下午 12:00-18:00",
    "evening": "今天晚上 18:00-24:00",
    "yesterday": "昨天全天",
    "last_night": "昨晚 18:00-24:00",
    "this_month": "本月 1 日 00:00 至今",
    "last_month": "上月 1 日 00:00 至本月 1 日 00:00",
    "last_1h": "最近 1 小时",
    "last_24h": "最近 24 小时",
    "last_7d": "最近 7 天",
    "last_30d": "最近 30 天",
}

# 工具 schema 的时段枚举统一从这里取，避免四处手写后漂移
PERIOD_ENUM = [
    "today",
    "early_morning",
    "morning",
    "noon",
    "afternoon",
    "evening",
    "yesterday",
    "last_night",
    "this_month",
    "last_month",
    "last_1h",
    "last_24h",
    "last_7d",
    "last_30d",
]
PERIOD_HELP = (
    "取值：today/early_morning(凌晨0-6)/morning(上午0-12)/noon(中午11-14)/"
    "afternoon(下午12-18)/evening(晚上18-24)/yesterday/last_night(昨晚18-24)/"
    "this_month/last_month/last_1h/last_24h/last_7d/last_30d；"
    "中文口语也直接接受（中午/凌晨/上午/下午/傍晚/晚上/昨晚/今天/昨天/本月）。"
)

# 中文口语 → (星期偏移天数, 起始小时, 结束小时)。长词在前，避免"昨天晚上"被"晚上"抢先。
_CN_TOD = (
    ("昨天晚上", -1, 18, 24),
    ("昨晚", -1, 18, 24),
    ("昨天下午", -1, 12, 18),
    ("昨天上午", -1, 0, 12),
    ("今天中午", 0, 11, 14),
    ("今天下午", 0, 12, 18),
    ("今天上午", 0, 0, 12),
    ("今天凌晨", 0, 0, 6),
    ("今天晚上", 0, 18, 24),
    ("凌晨", 0, 0, 6),
    ("半夜", 0, 0, 6),
    ("深夜", 0, 0, 6),
    ("清晨", 0, 0, 6),
    ("早晨", 0, 0, 12),
    ("早上", 0, 0, 12),
    ("上午", 0, 0, 12),
    ("中午", 0, 11, 14),
    ("正午", 0, 11, 14),
    ("下午", 0, 12, 18),
    ("午后", 0, 12, 18),
    ("傍晚", 0, 17, 20),
    ("晚上", 0, 18, 24),
    ("晚间", 0, 18, 24),
    ("夜里", 0, 18, 24),
)

_CN_DAYS = (("前天", -2), ("昨天", -1), ("昨日", -1), ("今天", 0), ("今日", 0), ("本日", 0))


def _parse_cn_period(text: str):
    """把中文口语时段解析成 (time_from, time_till, 描述)；不认识则返回 None。

    模型很自然会直接传 "中午"。旧实现遇到不认识的时段会**静默退化成"最近 1 天"**，
    于是模型把 24 小时的事件说成"中午的告警"——生产上真实发生过（附录 A 第 18 条）。
    """
    s = (text or "").strip()
    if not s:
        return None
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for word, day_off, h_from, h_till in _CN_TOD:
        if word in s:
            base = today0 + timedelta(days=day_off)
            start = base + timedelta(hours=h_from)
            end = base + timedelta(hours=h_till)
            return (
                int(start.timestamp()),
                int(end.timestamp()),
                "%s %02d:00-%02d:00" % (word, h_from, h_till),
            )

    for word, day_off in _CN_DAYS:
        if word in s:
            base = today0 + timedelta(days=day_off)
            if day_off == 0:
                return int(base.timestamp()), _now_ts(), "今天 00:00 至今"
            return (
                int(base.timestamp()),
                int((base + timedelta(days=1)).timestamp()),
                "%s全天" % word,
            )
    return None


def period_supported(period: str) -> bool:
    """这个时段名我们认不认。供上层显式提示模型，而不是悄悄换一个时段。"""
    key = (period or "").strip().lower()
    if not key:
        return True
    if key in PERIODS or key in ("this_morning", "month"):
        return True
    return _parse_cn_period(period) is not None


def resolve_period(period: str, days: int) -> tuple:
    """把时间段名解析为 (time_from, time_till, 人类可读窗口描述)。

    以服务端本地时区为准（Zabbix 前端可能按用户时区显示，两者可能有差异）。

    **不认识的时段绝不静默替换**：旧实现直接落到"最近 N 天"，返回的描述里
    也看不出来，结果模型拿 24 小时的清单回答"中午的告警"。现在描述里必须留下痕迹。
    """
    now = datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    key = (period or "").strip().lower()

    if key == "today":
        return int(today0.timestamp()), _now_ts(), PERIODS["today"]
    elif key in ("early_morning", "dawn"):
        return (
            int(today0.timestamp()),
            int((today0 + timedelta(hours=6)).timestamp()),
            PERIODS["early_morning"],
        )
    elif key in ("morning", "this_morning"):
        return (
            int(today0.timestamp()),
            int((today0 + timedelta(hours=12)).timestamp()),
            PERIODS["morning"],
        )
    elif key in ("noon", "midday", "lunch"):
        return (
            int((today0 + timedelta(hours=11)).timestamp()),
            int((today0 + timedelta(hours=14)).timestamp()),
            PERIODS["noon"],
        )
    elif key == "afternoon":
        return (
            int((today0 + timedelta(hours=12)).timestamp()),
            int((today0 + timedelta(hours=18)).timestamp()),
            PERIODS["afternoon"],
        )
    elif key in ("evening", "night"):
        return (
            int((today0 + timedelta(hours=18)).timestamp()),
            int((today0 + timedelta(days=1)).timestamp()),
            PERIODS["evening"],
        )
    elif key == "yesterday":
        start = today0 - timedelta(days=1)
        return int(start.timestamp()), int(today0.timestamp()), PERIODS["yesterday"]
    elif key == "last_night":
        start = today0 - timedelta(hours=6)
        return int(start.timestamp()), int(today0.timestamp()), PERIODS["last_night"]
    elif key in ("this_month", "month"):
        return int(today0.replace(day=1).timestamp()), _now_ts(), PERIODS["this_month"]
    elif key == "last_month":
        first_this = today0.replace(day=1)
        first_prev = (first_this - timedelta(days=1)).replace(day=1)
        return int(first_prev.timestamp()), int(first_this.timestamp()), PERIODS["last_month"]
    elif key == "last_1h":
        return _now_ts() - 3600, _now_ts(), PERIODS["last_1h"]
    elif key == "last_24h":
        return _now_ts() - 86400, _now_ts(), PERIODS["last_24h"]
    elif key == "last_7d":
        return _now_ts() - 7 * 86400, _now_ts(), PERIODS["last_7d"]
    elif key == "last_30d":
        return _now_ts() - 30 * 86400, _now_ts(), PERIODS["last_30d"]

    cn = _parse_cn_period(period)
    if cn:
        return cn

    d = max(1, int(days))
    if key:
        return (
            _days_ago(d),
            _now_ts(),
            "最近 %d 天（⚠ 未能识别时段 %r，已按最近 %d 天查询）" % (d, period, d),
        )
    return _days_ago(d), _now_ts(), "最近 %d 天" % d


def _fmt_local(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "?"


# ---- 监控项清单与取值 ----
# value_type: 0=float 1=char 2=log 3=unsigned 4=text，只有 0/3 有趋势数据
NUMERIC_VALUE_TYPES = (0, 3)
ITEM_DISABLED = "1"


def _key_family(key: str) -> str:
    """把监控项 key 归并成"族"，用于 key 没猜中时给出真实可用的 key。

    net.if.in[ifHCInOctets.15] -> net.if.in
    vm.memory.free[memAvailReal.0] -> vm.memory.free
    """
    return (key or "").split("[", 1)[0].strip()


def _lastclock(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("lastclock") or 0)
    except (TypeError, ValueError):
        return 0


def _usable_items(items: List[Dict[str, Any]], *, enabled_only: bool) -> List[Dict[str, Any]]:
    """剔除不可能有数据的项，并把"启用且有数据"的排到前面。

    flags: 0=普通 1=LLD规则 2=原型 4=LLD生成（item.get 默认只返回 0/4）。
    排序按 (是否禁用, 最近采集时间倒序)：这样候选里优先出现真实在采数据的项，
    而不是一堆禁用项或从没采到过数据的项。
    """
    out = []
    for it in items:
        if str(it.get("flags", "0")) not in ("0", "4"):
            continue
        if enabled_only and str(it.get("status", "0")) == ITEM_DISABLED:
            continue
        out.append(it)
    out.sort(
        key=lambda it: (
            str(it.get("status", "0")) == ITEM_DISABLED,
            -_lastclock(it),
            str(it.get("name") or ""),
        )
    )
    return out


def _key_families(items: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    """按 key 族汇总主机真实存在的监控项。

    这是"模型猜不到 key"这个失败模式的正面解法：把该主机真实存在的 key 交给它，
    而不是回一个空列表让它继续瞎猜。
    """
    fams: Dict[str, Dict[str, Any]] = {}
    for it in items:
        fam = _key_family(str(it.get("key_") or ""))
        if not fam:
            continue
        row = fams.setdefault(
            fam,
            {
                "family": fam,
                "count": 0,
                "units": it.get("units") or None,
                "sample_key": it.get("key_"),
                "sample_name": it.get("name"),
                "sample_last": it.get("lastvalue"),
                "_has_data": False,
            },
        )
        row["count"] += 1
        # 优先拿"采到过数据"的项当样本，模型据此能直接照抄真实 key
        if not row["_has_data"] and _lastclock(it) > 0:
            row["_has_data"] = True
            row["sample_key"] = it.get("key_")
            row["sample_name"] = it.get("name")
            row["sample_last"] = it.get("lastvalue")
    rows = sorted(fams.values(), key=lambda r: (-r["count"], r["family"]))[:limit]
    for row in rows:
        row.pop("_has_data", None)
    return rows


def _relax_key_hint(key_hint: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """key 未命中时的宽容匹配：去掉 [..] 参数，再按 . 分段逐级截短。

    实测模型会给出 net.if.incoming.bytes，而真实 key 是 net.if.in[ifHCInOctets.15]；
    放宽到 net.if 就能命中，省掉一整轮对话。
    """
    stripped = key_hint.split("[", 1)[0].strip()
    candidates = []
    if stripped and stripped.lower() != key_hint.lower():
        candidates.append(stripped)
    parts = [p for p in stripped.split(".") if p]
    for n in range(len(parts) - 1, 1, -1):
        candidates.append(".".join(parts[:n]))

    for cand in candidates:
        if len(cand) < 3:
            continue
        hit = [it for it in items if cand.lower() in str(it.get("key_") or "").lower()]
        if hit:
            logger.info("key 关键字放宽命中: %r -> %r（%d 项）", key_hint, cand, len(hit))
            return hit
    return []


def _num(value: float) -> float:
    """数值格式化：小数值留 4 位，大数值（如字节数）留 1 位。"""
    return round(value, 4) if abs(value) < 1000 else round(value, 1)


def _item_series(
    ctx: ToolContext, item: Dict[str, Any], *, time_from: int, time_till: int
) -> Dict[str, Any]:
    """取某监控项在时间窗内的数值序列。

    优先 trend 表（Zabbix 按整点写入小时级 min/avg/max），没有则回退 history。
    返回 {"points": [(clock, value)], "source", "min", "max", "samples"} 或 {"error"}。
    """
    itemid = str(item["itemid"])
    vt = int(item.get("value_type", 0) or 0)
    if vt not in NUMERIC_VALUE_TYPES:
        return {"error": f"非数值型监控项（value_type={vt}），没有趋势/历史数值"}

    rows = ctx.zclient.trends(ctx.sessionid, [itemid], time_from=time_from, time_till=time_till)
    if rows:
        ordered = sorted(rows, key=lambda r: int(r["clock"]))
        # value_min/value_max 是我们显式请求的字段；用 .get 兜底只是不让某个
        # 缺失字段把整个工具打崩（缺失时按该小时均值退化处理）。
        mins = [float(r.get("value_min", r["value_avg"])) for r in ordered]
        maxs = [float(r.get("value_max", r["value_avg"])) for r in ordered]
        return {
            "points": [(int(r["clock"]), float(r["value_avg"])) for r in ordered],
            "source": "trend",
            "min": min(mins),
            "max": max(maxs),
            "samples": len(ordered),
        }

    # 趋势表按整点写入：窗口过短或新装环境可能一行都没有，回退原始历史。
    hist = ctx.zclient.history(
        ctx.sessionid,
        [itemid],
        value_type=vt,
        time_from=time_from,
        time_till=time_till,
        limit=5000,
    )
    if not hist:
        return {"error": "该时间窗内没有数据（趋势表与历史表均为空）"}
    ordered = sorted(hist, key=lambda r: int(r["clock"]))
    values = [float(r["value"]) for r in ordered]
    return {
        "points": [(int(r["clock"]), v) for r, v in zip(ordered, values)],
        "source": "history",
        "min": min(values),
        "max": max(values),
        "samples": len(values),
    }


def _window_stats(
    ctx: ToolContext, item: Dict[str, Any], *, time_from: int, time_till: int
) -> Dict[str, Any]:
    """某监控项在时间窗内的统计（min/max/avg/first/last）。

    存在的理由：旧 query_metrics 只给 lastvalue，回答不了
    "告警那个时间段的带宽是多少"这类问题。
    """
    row: Dict[str, Any] = {
        "name": item.get("name"),
        "key": item.get("key_"),
        "units": item.get("units") or None,
    }
    if str(item.get("status", "0")) == ITEM_DISABLED:
        row["status"] = "已禁用"
    try:
        series = _item_series(ctx, item, time_from=time_from, time_till=time_till)
    except (ZabbixError, ValueError, TypeError) as exc:
        row["error"] = str(exc)
        return row
    if "error" in series:
        row["error"] = series["error"]
        return row
    points = series["points"]
    row.update(
        {
            "min": _num(series["min"]),
            "max": _num(series["max"]),
            "avg": _num(sum(v for _, v in points) / len(points)),
            "first": _num(points[0][1]),
            "last": _num(points[-1][1]),
            "samples": series["samples"],
            "source": series["source"],
        }
    )
    return row


def _pick_host(ctx: ToolContext, name: str) -> Optional[Dict[str, Any]]:
    """按名字挑主机：完全同名优先，其次前缀匹配，最后取第一个。

    hosts_by_name 走 Zabbix search（子串、忽略大小写），查 DEMO_SDWAN01
    可能同时命中 DEMO_SDWAN01 与 DEMO_SDWAN01_WAN01，这里做确定性选择。
    """
    hosts = ctx.zclient.hosts_by_name(ctx.sessionid, name)
    if not hosts:
        return None
    want = (name or "").strip().lower()
    for h in hosts:
        if (h.get("name") or "").strip().lower() == want:
            return h
    for h in hosts:
        if (h.get("name") or "").strip().lower().startswith(want):
            return h
    return hosts[0]


def _resolve_hostids(ctx: ToolContext, host_arg: str) -> tuple:
    """把主机名解析成 hostid 列表，返回 (hostids, error_dict)。"""
    want = (host_arg or "").strip()
    if not want:
        return [], None
    try:
        hosts = ctx.zclient.hosts_by_name(ctx.sessionid, want)
    except ZabbixError as exc:
        return [], {"error": f"解析主机名失败: {exc}"}
    if not hosts:
        return [], {"error": f"未找到主机: {want}"}
    exact = [h for h in hosts if (h.get("name") or "").strip().lower() == want.lower()]
    chosen = exact or hosts[:5]
    return [str(h["hostid"]) for h in chosen], None


# ---- 工具实现 ----
def tool_query_problems(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """查询故障/告警。

    行为约定（尽量不依赖模型选对 scope）：
    - **只要给了时间范围（period 或 days）**：查该时段内发生过的**全部故障，含已恢复**。
      这与用户直觉一致——问"今天有哪些告警"通常是想看全量；每行都带
      「未恢复 / 已恢复@时间」标记，模型可据此区分。
    - **不给时间段且 scope=current**：返回当前仍在报警的全部未恢复问题。
    - 不给时间段且不给 scope：按当前未恢复处理（与历史行为兼容）。

    实现要点：problem.get 只查未恢复（Zabbix 源码里是 r_eventid IS NULL），
    加 recent=true 也只多带"最近 OK_PERIOD 内恢复"的（生产常见 5m），
    无法回答"今天上午有哪些故障"。因此带时间范围的查询走
    event.get + r_eventid（事件表保留期由 housekeeping 决定，常见 365 天）。
    """
    scope = (args.get("scope") or "").strip().lower()
    period = str(args.get("period") or "").strip()
    period_given = bool(period)
    days_given = args.get("days") is not None
    limit = int(args.get("limit", 50))

    host_arg = str(args.get("host") or "").strip()
    hostids, host_err = _resolve_hostids(ctx, host_arg)
    if host_err:
        return host_err

    explicit_current = scope == "current" and not period_given and not days_given
    want_history = (not explicit_current) and (
        period_given or days_given or scope in ("history", "all", "resolved", "past")
    )

    if not want_history:
        # 当前仍在报警（不带时间范围）
        days = int(args.get("days", 1))
        problems = ctx.zclient.problems(
            ctx.sessionid,
            time_from=_days_ago(days),
            time_till=_now_ts(),
            limit=limit,
            hostids=hostids,
        )
        # problem.get 不返回主机（它根本不支持 selectHosts），必须用 triggerid 反查，
        # 否则模型只能从触发器名字里猜是哪台机器——模板触发器重名时会张冠李戴。
        trigger_ids = sorted({str(p.get("objectid")) for p in problems if p.get("objectid")})
        try:
            trigger_hosts = ctx.zclient.hosts_for_triggers(ctx.sessionid, trigger_ids)
        except ZabbixError as exc:
            logger.warning("反查触发器主机失败（主机名将为空）: %s", exc)
            trigger_hosts = {}

        rows = []
        for p in problems:
            rows.append({
                "eventid": p.get("eventid"),
                "name": p.get("name"),
                "hosts": trigger_hosts.get(str(p.get("objectid")), []),
                "severity": SEVERITY[min(int(p.get("severity", 0)), 5)],
                "occurred": _fmt_local(p.get("clock")),
                "state": "未恢复",
                # Zabbix 返回字符串 "0"/"1"，不能用 bool() 判断（bool("0") 为 True）
                "ack": str(p.get("acknowledged", "0")) == "1",
            })
        out: Dict[str, Any] = {
            "scope": "current",
            "host": host_arg or None,
            "count": len(rows),
            "note": "仅包含当前未恢复的问题（未指定时间段）。",
            "problems": rows,
        }
        if len(rows) >= limit:
            # 静默截断会让模型把"前 N 条"当成全部，必须说清楚
            out["truncated"] = True
            out["warning"] = (
                "结果达到条数上限 %d，可能还有未返回的问题；"
                "请缩小范围或提高 limit 后重查，并在回答里说明只列了前 %d 条。" % (limit, limit)
            )
        if not rows:
            out["hint"] = (
                "如需查看某时间段内发生过的全部故障（含已恢复），"
                "请给出 period，例如 today/morning/noon/afternoon/yesterday。"
            )
        return out

    # ---- 历史：含已恢复 ----
    time_from, time_till, desc = resolve_period(args.get("period"), args.get("days", 1))
    events = ctx.zclient.events(
        ctx.sessionid,
        time_from=time_from,
        time_till=time_till,
        value=1,
        limit=limit,
        hostids=hostids,
    )

    # r_eventid 非 "0" 表示已恢复；再用一次 event.get 取恢复时间
    recovery_ids = [
        e["r_eventid"]
        for e in events
        if str(e.get("r_eventid") or "0") not in ("0", "", "None")
    ]
    recovery_clock: Dict[str, int] = {}
    if recovery_ids:
        for r in ctx.zclient.events_by_ids(ctx.sessionid, recovery_ids):
            recovery_clock[str(r.get("eventid"))] = int(r.get("clock", 0))

    now_ts = _now_ts()
    rows = []
    active = resolved = 0
    for e in events:
        occurred = int(e.get("clock", 0))
        rid = str(e.get("r_eventid") or "0")
        if rid in ("0", "", "None"):
            state = "未恢复"
            resolved_at = None
            duration = now_ts - occurred
            active += 1
        else:
            state = "已恢复"
            resolved_at = recovery_clock.get(rid)
            duration = (resolved_at - occurred) if resolved_at else None
            resolved += 1
        rows.append({
            "eventid": e.get("eventid"),
            "name": e.get("name"),
            "hosts": [h.get("name") for h in (e.get("hosts") or [])],
            "severity": SEVERITY[min(int(e.get("severity", 0)), 5)],
            "occurred": _fmt_local(occurred),
            "state": state,
            "resolved_at": _fmt_local(resolved_at) if resolved_at else None,
            "duration_min": round(duration / 60.0, 1) if duration is not None else None,
            "ack": str(e.get("acknowledged", "0")) == "1",
        })

    result: Dict[str, Any] = {
        "scope": "history",
        "host": host_arg or None,
        "window": {
            "from": _fmt_local(time_from),
            "to": _fmt_local(time_till),
            "description": desc,
            "timezone": "服务端本地时区",
        },
        "count": len(rows),
        "active": active,
        "resolved": resolved,
        "note": (
            "已包含已恢复的故障。数据源为 Zabbix 事件表，保留期由 housekeeping 决定"
            "（常见为 365 天）。"
        ),
        "problems": rows,
    }
    warnings: List[str] = []
    if period_given and not period_supported(period):
        # 绝不静默换时段：把"我没听懂"明确交给模型，否则它会把 24 小时的事件说成"中午的告警"
        result["period_unrecognized"] = True
        result["supported_periods"] = PERIOD_ENUM
        warnings.append(
            f"未能识别时段 {period!r}，本次实际按「{desc}」查询。"
            "请在回答里如实说明这个时间范围，或改用受支持的时段重查。"
        )
    if len(rows) >= limit:
        result["truncated"] = True
        warnings.append(
            f"结果达到条数上限 {limit}，可能还有未返回的事件；"
            f"请缩小时间段或提高 limit 后重查，并在回答里说明只列了前 {limit} 条。"
        )
    if warnings:
        result["warning"] = " ".join(warnings)
    if not rows:
        result["hint"] = (
            f"{host_arg + ' 在' if host_arg else ''}{desc} 内没有发生任何故障事件。"
            "若想看当前仍在报警的问题（可能开始于该时段之前），请只传 scope=current 且不带时间段。"
        )
    return result



def tool_query_top(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """统计某时间段内告警最多的主机 / 触发器 TOP N（服务端聚合）。

    为什么必须服务端统计：真实事故中把原始事件清单交给模型自己数，
    模型只在被 limit 截断的 10 条上统计，报出"最多的设备 3 次"，
    而实际有主机高达 40 次，与 Zabbix 报告完全对不上。
    本工具会分页扫描该时段**全部**问题事件后再聚合排名。
    """
    top_n = max(1, int(args.get("top", 10)))
    time_from, time_till, desc = resolve_period(args.get("period"), args.get("days", 30))

    events, truncated = ctx.zclient.events_window_all(
        ctx.sessionid, time_from=time_from, time_till=time_till
    )

    host_stats: Dict[str, Dict[str, Any]] = {}
    trigger_stats: Dict[str, int] = {}
    severity_stats: Dict[str, int] = {}
    active_total = resolved_total = 0

    for e in events:
        rid = str(e.get("r_eventid") or "0")
        is_resolved = rid not in ("0", "", "None")
        if is_resolved:
            resolved_total += 1
        else:
            active_total += 1

        names = [
            (h.get("name") or h.get("host") or "(未命名主机)")
            for h in (e.get("hosts") or [])
        ] or ["(未指定主机)"]
        for name in set(names):
            st = host_stats.setdefault(
                name, {"host": name, "count": 0, "active": 0, "resolved": 0}
            )
            st["count"] += 1
            st["resolved" if is_resolved else "active"] += 1

        tname = e.get("name") or "(未命名触发器)"
        trigger_stats[tname] = trigger_stats.get(tname, 0) + 1
        sev = SEVERITY[min(int(e.get("severity", 0)), 5)]
        severity_stats[sev] = severity_stats.get(sev, 0) + 1

    top_hosts = sorted(host_stats.values(), key=lambda x: (-x["count"], x["host"]))[:top_n]
    top_triggers = sorted(
        ({"trigger": k, "count": v} for k, v in trigger_stats.items()),
        key=lambda x: (-x["count"], x["trigger"]),
    )[:top_n]

    out: Dict[str, Any] = {
        "window": {
            "from": _fmt_local(time_from),
            "to": _fmt_local(time_till),
            "description": desc,
            "timezone": "服务端本地时区",
        },
        "scanned_events": len(events),
        "active": active_total,
        "resolved": resolved_total,
        "distinct_hosts": len(host_stats),
        "top_hosts": top_hosts,
        "top_triggers": top_triggers,
        "severity_distribution": severity_stats,
        "note": (
            "统计在服务端完成，已扫描该时段全部问题事件（含已恢复）。"
            "top_hosts 含每台的未恢复/已恢复拆分；top_triggers 按触发器名统计。"
        ),
    }
    if truncated:
        out["truncated"] = True
        out["warning"] = (
            f"事件数超过扫描上限，仅统计了前 {len(events)} 条，结果可能不完整；"
            "请缩小时间段后重试。"
        )
    if not events:
        out["hint"] = f"{desc} 内没有任何故障事件。"
    return out


def tool_query_metrics(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """查某主机监控项的取值：最近值，或指定时间段内的统计。

    为什么这么设计（生产实测的失败模式）：生产 657 台主机混着 agent 与 SNMP 设备，
    两者 key 命名完全不同（Linux 是 system.cpu.load，SNMP 是 net.if.in[ifHCInOctets.15]）。
    模型并不知道某台主机有哪些 key，实测它连续猜 3 个不存在的 key，而旧实现每次都只
    回 {"items": []}——没有任何线索，模型只能放弃并答复"无法获取数据"。
    **猜不到 key ≠ 没有数据**，因此这里在未命中时回传该主机真实存在的 key 族清单。
    """
    host_name = args.get("host") or args.get("name") or ""
    key_hint = str(args.get("key") or "").strip()
    if not host_name:
        return {"error": "需要 host 参数"}

    host = _pick_host(ctx, host_name)
    if not host:
        return {"error": f"未找到主机: {host_name}"}

    all_items = ctx.zclient.items_by_host(ctx.sessionid, str(host["hostid"]))
    usable = _usable_items(all_items, enabled_only=False)
    if not usable:
        return {
            "host": host.get("name"),
            "items_total": len(all_items),
            "hint": "该主机没有任何可用监控项（可能只挂了模板但未采集到项）。",
        }

    period = str(args.get("period") or "").strip()
    days_arg = args.get("days") if args.get("days") is not None else 1
    window_mode = bool(period) or args.get("days") is not None
    time_from, time_till, desc = resolve_period(period, days_arg)

    matches = usable
    relaxed_from = None
    if key_hint:
        matches = [it for it in usable if key_hint.lower() in str(it.get("key_") or "").lower()]
        if not matches:
            relaxed = _relax_key_hint(key_hint, usable)
            if relaxed:
                matches, relaxed_from = relaxed, key_hint

    base: Dict[str, Any] = {
        "host": host.get("name"),
        "items_total": len(all_items),
        "items_usable": len(usable),
    }

    # 完全没匹配上：把真实存在的 key 族交出去，而不是让模型继续瞎猜
    if not matches:
        base.update(
            {
                "key_query": key_hint,
                "matched": 0,
                "key_families": _key_families(usable),
                "hint": (
                    f"key 关键字 {key_hint!r} 没有匹配到任何监控项——这不代表该主机没有数据。"
                    "请从 key_families 里挑真实存在的 key（用它的子串即可，如 net.if.in），"
                    "再调用本工具。不要继续凭印象猜 key。"
                ),
            }
        )
        return base

    # 没给 key 也没给时间段：给"按族汇总"的概览，比丢 20 个接口项有用得多
    if not key_hint and not window_mode:
        base.update(
            {
                "key_families": _key_families(usable),
                "hint": (
                    "未指定 key，以上按 key 族汇总了该主机真实存在的监控项"
                    "（sample_key 是可直接使用的真实 key）。需要具体数值请带 key 再查一次。"
                ),
            }
        )
        return base

    def _rank(it: Dict[str, Any]):
        try:
            last = float(it.get("lastvalue") or 0)
        except (TypeError, ValueError):
            last = 0.0
        return (str(it.get("status", "0")) == ITEM_DISABLED, -last, -_lastclock(it))

    ranked = sorted(matches, key=_rank)
    by_family = False
    if window_mode and not key_hint:
        # 无 key 的时间段查询：每个 key 族取一个代表项，构成主机层面的运行状况概览
        picked, seen = [], set()
        for it in ranked:
            fam = _key_family(str(it.get("key_") or ""))
            if fam in seen:
                continue
            seen.add(fam)
            picked.append(it)
            if len(picked) >= 12:
                break
        selected, by_family = picked, True
    else:
        selected = ranked[: 12 if window_mode else 20]

    if window_mode:
        rows = [
            _window_stats(ctx, it, time_from=time_from, time_till=time_till) for it in selected
        ]
    else:
        rows = []
        for it in selected:
            row: Dict[str, Any] = {
                "name": it.get("name"),
                "key": it.get("key_"),
                "units": it.get("units") or None,
                "last": it.get("lastvalue"),
            }
            if _lastclock(it):
                row["last_seen"] = _fmt_local(_lastclock(it))
            if str(it.get("status", "0")) == ITEM_DISABLED:
                row["status"] = "已禁用"
            rows.append(row)

    out: Dict[str, Any] = dict(base)
    out.update(
        {
            "matched": len(matches),
            "returned": len(rows),
            "scope": "window" if window_mode else "current",
            "items": rows,
        }
    )
    if window_mode:
        out["window"] = {
            "from": _fmt_local(time_from),
            "to": _fmt_local(time_till),
            "description": desc,
            "timezone": "服务端本地时区",
        }
        if by_family:
            out["note"] = "未指定 key，每个 key 族取了一个代表项做时间段统计。"
    if relaxed_from:
        out["relaxed_from"] = relaxed_from
        out["note"] = f"key 关键字 {relaxed_from!r} 没有精确命中，已按放宽后的前缀匹配。"
    if len(matches) > len(rows) and not by_family:
        out["truncated"] = True
        out["key_families"] = _key_families(matches)
        out["hint"] = f"匹配到 {len(matches)} 项，仅返回最活跃的 {len(rows)} 项；如需其它项请用更精确的 key。"
    return out


def tool_trend_analysis(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """读 trend 数据算日增速并线性外推（如"磁盘还有几天满"）。

    候选项必须筛干净：生产上 DEMO_SDWAN01 有 198 个监控项、DEMO_SDWAN02 有 188 个，
    其中约一半是禁用的，另有 LLD 生成项与原型。旧实现直接取 items[:5]，抽到禁用项或
    没数据的项就报"数据不足"，用户看到的就是一句"无法获取数据"。
    """
    host_name = args.get("host") or args.get("name") or ""
    key_hint = str(args.get("key") or "").strip()
    total_cap = args.get("total") or args.get("capacity")
    if not host_name:
        return {"error": "需要 host 参数"}

    host = _pick_host(ctx, host_name)
    if not host:
        return {"error": f"未找到主机: {host_name}"}

    period = str(args.get("period") or "").strip()
    days_arg = args.get("days") if args.get("days") is not None else 14
    days = max(1, int(days_arg))
    if period:
        time_from, time_till, desc = resolve_period(period, days)
        days = max(1, int(round((time_till - time_from) / 86400.0)))
    else:
        time_from, time_till, desc = resolve_period("", days)

    all_items = ctx.zclient.items_by_host(ctx.sessionid, str(host["hostid"]))
    usable = [
        it
        for it in _usable_items(all_items, enabled_only=True)
        if int(it.get("value_type", 0) or 0) in NUMERIC_VALUE_TYPES
    ]
    candidates = usable
    if key_hint:
        candidates = [it for it in usable if key_hint.lower() in str(it.get("key_") or "").lower()]
        if not candidates:
            candidates = _relax_key_hint(key_hint, usable)

    window = {
        "from": _fmt_local(time_from),
        "to": _fmt_local(time_till),
        "description": desc,
        "timezone": "服务端本地时区",
    }

    if not candidates:
        return {
            "error": f"主机 {host_name} 没有匹配的数值型监控项（key 含 {key_hint!r}）",
            "window": window,
            "items_total": len(all_items),
            "key_families": _key_families(usable),
            "hint": (
                "请先用 query_metrics（不传 key）查看该主机真实存在的监控项，"
                "再用返回的 sample_key 子串重试；不要凭印象猜 key。"
            ),
        }

    tried: List[str] = []
    analyses: List[Dict[str, Any]] = []
    for it in candidates[:8]:
        tried.append(str(it.get("key_")))
        try:
            series = _item_series(ctx, it, time_from=time_from, time_till=time_till)
        except (ZabbixError, ValueError, TypeError) as exc:
            logger.info("趋势取值失败 %s: %s", it.get("key_"), exc)
            continue
        points = series.get("points") or []
        if len(points) < 2:
            continue

        first_ts, first_val = points[0]
        last_ts, last_val = points[-1]
        span_sec = max(60.0, float(last_ts - first_ts))
        daily_growth = (last_val - first_val) / (span_sec / 86400.0)

        row: Dict[str, Any] = {
            "item": it.get("name"),
            "key": it.get("key_"),
            "units": it.get("units") or None,
            "source": series["source"],  # trend=趋势表(按小时) / history=原始历史
            "first_value": _num(first_val),
            "last_value": _num(last_val),
            "min": _num(series["min"]),
            "max": _num(series["max"]),
            "span_hours": round(span_sec / 3600.0, 2),
            "daily_growth": _num(daily_growth),
            "samples": series["samples"],
            "days_to_full": None,
        }
        if series["source"] == "history":
            row["note"] = "趋势表暂无数据，已改用原始历史估算；时间跨度较短，结果仅供参考"
        if total_cap is not None:
            try:
                cap = float(total_cap)
                if daily_growth > 0 and cap > last_val:
                    row["days_to_full"] = round((cap - last_val) / daily_growth, 1)
            except (TypeError, ValueError):
                pass
        analyses.append(row)

    if not analyses:
        return {
            "error": f"主机 {host_name} 在{desc}内没有可用于趋势分析的监控项数据",
            "window": window,
            "items_total": len(all_items),
            "tried_keys": tried,
            "key_families": _key_families(usable),
            "hint": (
                "已尝试的 key 都取不到数据。请先用 query_metrics（不传 key）查看该主机"
                "真实存在的监控项与最近值，再选一个确实在采集的 key 重试。"
            ),
        }

    # 变化最剧烈的排前面，最多给 3 条（够模型组织"运行状况"的叙述）
    analyses.sort(key=lambda r: -abs(r["daily_growth"]))
    out: Dict[str, Any] = {
        "host": host.get("name"),
        "window": window,
        "candidates_tried": len(tried),
        "analyses": analyses[:3],
        "analysis": analyses[0],
    }
    if analyses[0]["source"] == "history":
        out["note"] = "趋势表暂无数据，已改用原始历史估算；时间跨度较短，结果仅供参考。"
    if len(tried) > len(analyses):
        out["skipped_no_data"] = len(tried) - len(analyses)
    return out


def tool_send_dingtalk(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """发消息到供应商钉钉群。供应商名必须在注册表白名单内。

    按供应商配置自动处理安全设置：加签（secret）/ 自定义关键词（keyword）。
    注意：必须是同步函数——工具调用链是同步的，返回 coroutine 会导致序列化失败。
    """
    supplier = (args.get("supplier") or "").strip()
    message = (args.get("message") or "").strip()
    names = ", ".join(ctx.suppliers.keys())

    if not supplier:
        return {"error": f"需要 supplier 参数。已注册供应商: {names}"}

    target = ctx.suppliers.get(supplier)
    if target is None:
        # 唯一前缀匹配（歧义时拒绝，避免误发给错误供应商）
        hits = [t for t in ctx.suppliers.values() if t.name.startswith(supplier)]
        if len(hits) == 1:
            target = hits[0]
        else:
            return {"error": f"未注册供应商: {supplier}。可选: {names}"}

    if not message:
        return {"error": "message 不能为空"}

    # 消息模板优先级：供应商级 message_template > 全局 dingtalk.message_template > 内置默认
    template = target.message_template or ctx.message_template
    content = render_message(template, ctx.username, message)
    try:
        send_text(
            target.webhook,
            content,
            secret=target.secret or None,
            keyword=target.keyword or None,
        )
    except DingtalkError as exc:
        ctx.audit(ctx.username, "send_dingtalk", target.name, f"FAILED {exc}")
        return {"error": str(exc)}

    ctx.audit(ctx.username, "send_dingtalk", target.name, f"OK: {message[:200]}")
    return {
        "ok": True,
        "supplier": target.name,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }


INTERFACE_TYPE_LABEL = {"1": "agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}


def _pick_interface(interfaces: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """挑一个最能代表该主机的接口：优先 main=1，其次按 agent/SNMP/IPMI/JMX 排。

    内部主机（如 Zabbix server 自身）没有接口，此时返回 None。
    """
    if not interfaces:
        return None
    type_rank = {"1": 0, "2": 1, "3": 2, "4": 3}

    def rank(iface: Dict[str, Any]) -> tuple:
        is_main = 0 if str(iface.get("main", "0")) == "1" else 1
        return (is_main, type_rank.get(str(iface.get("type", "")), 9))

    return sorted(interfaces, key=rank)[0]


def tool_query_hosts(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """列出主机并给出整体运行状况概览。

    用于回答"监控了哪些主机""zabbix 当前运行状况"这类问题，
    避免模型在没有工具可用时编造答案。
    """
    search = (args.get("search") or "").strip()
    days = max(1, int(args.get("days", 7)))

    try:
        hosts = ctx.zclient.hosts(ctx.sessionid, search=search or None)
    except ZabbixError as exc:
        return {"error": f"查询主机失败: {exc}"}

    status_map = {"0": "监控中", "1": "已禁用"}
    avail_map = {"0": "未知", "1": "可用", "2": "不可用"}

    rows = []
    monitored = disabled = unavailable = 0
    for h in hosts:
        status = status_map.get(str(h.get("status")), "未知")
        if status == "监控中":
            monitored += 1
        elif status == "已禁用":
            disabled += 1
        row = {"name": h.get("name"), "status": status}
        # 接口信息：模型回答「这台机器 IP 是多少」时的唯一数据来源
        iface = _pick_interface(h.get("interfaces") or [])
        if iface:
            # useip=0 表示该主机走 DNS 名而不是 IP
            if str(iface.get("useip", "1")) == "0":
                addr = iface.get("dns") or iface.get("ip")
            else:
                addr = iface.get("ip") or iface.get("dns")
            if addr:
                row["ip"] = addr
            if iface.get("port"):
                row["port"] = iface["port"]
            label = INTERFACE_TYPE_LABEL.get(str(iface.get("type", "")))
            if label:
                row["interface_type"] = label
        # 内部主机（如 Zabbix server 自身）没有接口，available 字段可能缺失
        if h.get("available") is not None:
            available = avail_map.get(str(h.get("available")), "未知")
            row["available"] = available
            if available == "不可用":
                unavailable += 1
        rows.append(row)

    overview: Dict[str, Any] = {
        "host_count": len(rows),
        "monitored": monitored,
        "disabled": disabled,
        "unavailable": unavailable,
    }
    try:
        probs = ctx.zclient.problems(
            ctx.sessionid, time_from=_days_ago(days), time_till=_now_ts(), limit=500
        )
        by_sev: Dict[str, int] = {}
        for p in probs:
            label = SEVERITY[min(int(p.get("severity", 0)), 5)]
            by_sev[label] = by_sev.get(label, 0) + 1
        overview[f"problems_last_{days}d"] = len(probs)
        overview["problems_by_severity"] = by_sev
    except ZabbixError as exc:
        overview["problems_error"] = str(exc)

    return {"overview": overview, "hosts": rows[:50], "truncated": len(rows) > 50}


# ---- 工具注册表 ----
def build_tool_schemas() -> List[Tool]:
    return [
        {
            "name": "query_problems",
            "description": (
                "查询故障/告警。"
                "**只要传入时间段（period 或 days），返回的就是该时段内发生过的全部故障，"
                "包含已恢复的**（每行带 state=未恢复/已恢复 与 resolved_at、duration_min），"
                "适合「今天上午有什么故障」「今天到目前为止有哪些告警」「昨天出了哪些问题」；"
                "period 可选 today/morning(今天上午)/afternoon/yesterday/"
                "last_1h/last_24h/last_7d/last_30d；"
                "若只想知道**此刻仍在报警**的问题（不论何时开始），"
                "请只传 scope=current 且不要传时间段。"
                "问「某台主机」的故障要传 host（例如 host=DEMO_SDWAN01、period=this_month）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": PERIOD_ENUM,
                        "description": "时间段；给出即包含已恢复的故障。" + PERIOD_HELP,
                    },
                    "days": {"type": "integer", "description": "回溯天数（等价于时间段，同样含已恢复）"},
                    "host": {
                        "type": "string",
                        "description": "只看某台主机的故障（主机名，支持子串）；问「某台主机本月/今天的告警」时必传",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["current", "history"],
                        "description": "current=仅当前未恢复（需不传时间段）；history=时间段内全部（含已恢复）",
                    },
                    "limit": {"type": "integer", "description": "返回条数，默认50"},
                },
            },
        },
        {
            "name": "query_top",
            "description": (
                "统计某时间段内**告警最多的主机 / 触发器 TOP N**（服务端聚合，已扫描全部事件）。"
                "凡是「哪台设备告警最多」「TOP10」「告警排名」「谁最吵」这类统计排名问题，**必须用本工具**，"
                "不要用 query_problems 拿清单自己数（清单会被截断，导致排名完全错误）。"
                "period 支持 this_month(本月)/last_month/last_7d/last_30d/today/yesterday 等；"
                "本月用 this_month（与 Zabbix 报告口径一致）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": PERIOD_ENUM,
                        "description": "统计时间段；本月=this_month。" + PERIOD_HELP,
                    },
                    "days": {"type": "integer", "description": "回溯天数（未给 period 时使用，默认30）"},
                    "top": {"type": "integer", "description": "取前几名，默认10"},
                },
            },
        },
        {
            "name": "query_metrics",
            "description": (
                "查询某台主机监控项的取值：不带时间段时给最近值，带时间段时给该时段的"
                "min/max/avg/first/last。"
                "**不要凭印象猜 key**：agent 主机与 SNMP 设备的 key 命名完全不同"
                "（SNMP 设备是 net.if.in[ifHCInOctets.15] 这种形式）。"
                "不知道 key 时先不传 key 调用一次——返回的 key_families 就是该主机真实存在的 "
                "key，挑一个用它的子串（如 net.if.in）再查。"
                "问「某段时间的指标」要一起传 period，例如"
                "「告警时间段的带宽」= host + key=net.if.in + period=today。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "主机名（支持子串）"},
                    "key": {
                        "type": "string",
                        "description": (
                            "监控项 key 关键字（子串匹配），如 system.cpu.load、net.if.in、"
                            "vm.memory.available；不确定就留空"
                        ),
                    },
                    "period": {
                        "type": "string",
                        "enum": PERIOD_ENUM,
                        "description": "要统计的时间段；给了就返回该时段的 min/max/avg。" + PERIOD_HELP,
                    },
                    "days": {"type": "integer", "description": "回溯天数（未给 period 时使用）"},
                },
                "required": ["host"],
            },
        },
        {
            "name": "query_hosts",
            "description": (
                "列出被监控的主机并给出整体运行状况概览（主机数、监控中/已禁用/不可用、"
                "最近告警按严重度统计），有接口的主机会带 ip / port / interface_type。"
                "回答「监控了哪些主机」「当前运行状况」，以及「某台主机的 IP 地址 / 端口是多少」"
                "这类问题时使用；查询指定主机时传 search。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "主机名关键字（同时匹配显示名与技术名），可选",
                    },
                    "days": {"type": "integer", "description": "告警统计回溯天数，默认7"},
                },
            },
        },
        {
            "name": "trend_analysis",
            "description": (
                "读取某主机监控项的趋势数据，计算日增速并线性外推；可给容量上限(total)预测还有几天满。"
                "适合「磁盘/内存还有几天满」这类问题。不知道 key 时可以不传 key，"
                "工具会自动挑选该主机确实在采集数据的监控项；但**不要凭印象猜 key**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "主机名（支持子串）"},
                    "key": {"type": "string", "description": "监控项 key 关键字（子串）；不确定就留空"},
                    "period": {
                        "type": "string",
                        "enum": PERIOD_ENUM,
                        "description": "分析时间段，如 this_month。" + PERIOD_HELP,
                    },
                    "days": {"type": "integer", "description": "回溯天数（未给 period 时使用），默认14"},
                    "total": {"type": "number", "description": "容量/上限，用于预测满的时间"},
                },
                "required": ["host"],
            },
        },
    ]
    # 这里**故意不注册 send_dingtalk**。发送消息到供应商群是有真实外部副作用的
    # 动作，交给 LLM 自主决定已出现过两类事故：
    #   1) 未调用工具却回答"已发送成功"（编造成功）
    #   2) 与监控无关的问题（例如"今天上海的天气怎么样"）被它主动转发到供应商群
    # 因此发送只能由用户在输入框以「/供应商名 消息内容」触发，走 main.py 的
    # _handle_slash_command 确定性路径；LLM 侧不暴露任何发送能力。


def build_tool_handlers(ctx: ToolContext) -> Dict[str, Callable]:
    raw = {
        "query_problems": lambda a: tool_query_problems(ctx, a),
        "query_top": lambda a: tool_query_top(ctx, a),
        "query_metrics": lambda a: tool_query_metrics(ctx, a),
        "query_hosts": lambda a: tool_query_hosts(ctx, a),
        "trend_analysis": lambda a: tool_trend_analysis(ctx, a),
        # 不含 send_dingtalk：见 build_tool_schemas() 末尾的原因说明
    }

    def wrap(name: str, fn: Callable) -> Callable:
        """兜底 + 可观测性。

        兜底：若工具是协程函数，同步执行它——工具调用链是同步的，直接返回
        coroutine 会让 json 序列化失败，导致该工具在真实对话中永远不可用
        （单元测试若直接 await 则无法发现）。
        日志：记录每次工具调用的参数与结果，便于线上排查（含钉钉发送留痕）。
        """

        def inner(args):
            logger.info(
                "tool call: %s args=%s", name, json.dumps(args, ensure_ascii=False)[:500]
            )
            out = fn(args)
            if inspect.iscoroutine(out):
                out = asyncio.run(out)
            logger.info(
                "tool result: %s -> %s",
                name,
                json.dumps(out, ensure_ascii=False, default=str)[:500],
            )
            return out

        return inner

    return {name: wrap(name, fn) for name, fn in raw.items()}