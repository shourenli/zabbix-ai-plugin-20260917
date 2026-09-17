"""FastAPI 入口：AI 对话框伴生应用。

- 鉴权：校验 zbx_session cookie → 未登录/过期返回 401
- 对话：POST /api/chat 触发 LLM 工具调用循环
- 会话历史：按用户隔离（MySQL）
- 审计：记录钉钉发送动作
- 前端静态页：GET /（前端从同源 /ai/ 进入）
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .auth import AuthUnavailable, SessionError, authenticate
from .config import settings
from .llm import LLMClient, LLMError
from .storage import MySQLConfig, Storage, StorageError
from .tools import (
    ToolContext,
    build_tool_handlers,
    build_tool_schemas,
    tool_send_dingtalk,
)
from .zabbix import Unauthorized, ZabbixClient, ZabbixError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("zabbix_ai")

# /供应商名 消息内容
SLASH_RE = re.compile(r"^/(\S+)[ \t]+(.+)$", re.S)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
LICENSE_FILE = BASE_DIR / "LICENSE"
# GPL-3.0 §5(d) 惯例：有交互界面的作品应展示 Appropriate Legal Notices
COPYRIGHT_NOTICE = "Copyright (C) 2026 shourenli"

# ---- 依赖装配 ----
storage = Storage(
    MySQLConfig(
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
        user=settings.mysql_user,
        password=settings.mysql_password,
        charset=settings.mysql_charset,
    )
)
zclient = ZabbixClient(
    settings.api_url,
    service_user=os.environ.get("ZABBIX_SERVICE_USER"),
    service_password=os.environ.get("ZABBIX_SERVICE_PASSWORD"),
)
llm = LLMClient(
    settings.llm_base_url,
    settings.llm_model,
    settings.llm_api_key,
    temperature=settings.llm_temperature,
    top_p=settings.llm_top_p,
    max_tokens=settings.llm_max_tokens,
    function_calling=settings.llm_function_calling,
    extra_params=settings.llm_extra_params,
)

SYSTEM_PROMPT = (
    "你是 Zabbix 监控运维助手。你通过调用工具获取监控数据、分析趋势、向供应商钉钉群发送消息。"
    "回答要简洁、基于工具返回的真实数据。"
    "重要：只能依据工具返回的数据作答；若现有工具无法回答该问题（例如没有对应工具），"
    "必须明确说明无法查询，绝对不能编造或猜测数据。"
    "**你只有上面列出的这些工具，不存在其它工具（天气、搜索、上网、发消息等都没有）；"
    "绝不能编造工具名、伪造工具调用，也不能输出凭空想象的「调用结果」。**"
    "询问「有哪些主机」「运行状况」，或「某台主机的 IP 地址 / 端口」时调用 query_hosts"
    "（查询指定主机要传 search 参数，返回里带 ip/port）；"
    "询问告警时调用 query_problems：**只要问题里带时间范围（今天/上午/昨天/最近N天等），"
    "就传 period（或 days）**，这样会返回该时段全部故障——包含已恢复的，"
    "并给出每条的 state、resolved_at、duration_min；"
    "用户若明确只问「现在正在报警的」，才用 scope=current 且不传时间段。"
    "**时间词必须用受支持的时段表达，并说清实际查的范围**：中午=noon(11:00-14:00)、"
    "凌晨=early_morning(00:00-06:00)、上午/早上=morning、下午=afternoon(12:00-18:00)、"
    "傍晚/晚上=evening(18:00-24:00)、昨晚=last_night、本月=this_month；中文说法也可以直接传。"
    "**回答任何带时间的提问时，必须先说明你实际查询的时间范围**（用工具返回的 window.description）；"
    "如果工具返回了 period_unrecognized 或 warning，说明你没表达清楚或结果被截断，"
    "必须按提示改查或如实说明——**绝不能把「最近 1 天」说成「中午」，也不能把前 N 条说成全量**。"
    "另外，历史故障是按**事件发生时间**统计的，不代表此刻仍在报警，回答时要区分清楚。"
    "询问指标值调用 query_metrics：**绝对不要凭印象猜监控项的 key**——agent 主机与 SNMP "
    "设备的 key 命名完全不同（SNMP 是 net.if.in[ifHCInOctets.15] 这种形式）。"
    "不知道 key 时先不传 key 调用一次，返回的 key_families 就是该主机真实存在的 key，"
    "挑一个再用它的子串（如 net.if.in）重查；"
    "如果工具返回了 relaxed_from，说明你给的那个 key **并不存在**、工具是按放宽后的前缀匹配的，"
    "答复里必须如实说明（原 key 不存在，实际取的是哪个 key），"
    "绝不能把数值挂在那个不存在的 key 上；"
    "问某个时间段的指标（如「告警时间段的带宽使用情况」）要同时传 period；"
    "「某台主机本月运行状况」= query_problems(host=该主机, period=this_month) "
    "加上 query_metrics(host=该主机, period=this_month)。"
    "询问增长/何时耗尽（如磁盘还有几天满）调用 trend_analysis，同样不要猜 key。"
    "**工具返回 error 或 hint 时，必须按 hint 再试一次或如实说明缺什么，绝不能编造数值，"
    "也不能把「key 没猜中」说成「该监控项没有配置」。**"
    "**统计排名类问题（哪台设备告警最多、TOP10、告警排名）必须调用 query_top**，"
    "由服务端聚合后给结果，绝不要用 query_problems 的清单自己数——清单会被截断导致排名错误；"
    "「本月」对应 period=this_month。"
    "你没有发送消息的工具，也绝不能声称已发送、已通知或已转发给任何人。"
    "只有用户在输入框以「/供应商名 消息内容」开头时，系统才会确定性地发到对应"
    "供应商钉钉群（这一步不经过你，你只需如实说明系统会自动处理）。"
    "供应商白名单：" + (", ".join(settings.supplier_names()) or "（无）")
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时按配置确保表结构存在；失败只告警不阻断（便于先起服务再修配置）。

    但"不阻断"不等于"不吭声"：无论哪种模式，最后都做一次只读自检，
    存储不可用时必须在启动日志里留下 ERROR——否则问题会拖到用户提问才暴露
    （实测踩过：迁移失败被当 warning 吞掉，结果是每次对话 502）。
    """
    if settings.mysql_auto_init:
        try:
            storage.init_schema()
            logger.info("MySQL 表结构就绪: %s", storage.config.describe())
        except StorageError as exc:
            logger.warning("MySQL 初始化失败（服务仍启动）: %s", exc)
    else:
        logger.info("storage.mysql.auto_init=false，跳过自动建表（需 DBA 预先建表）")

    problem = storage.check_schema()
    if problem:
        logger.error("存储自检未通过，对话历史将不可用：%s", problem)
    else:
        purged = storage.purge_expired(settings.history_retention_days)
        if purged:
            logger.info(
                "已清理 %d 条超过 %d 天的历史对话记录",
                purged, settings.history_retention_days,
            )
    yield


app = FastAPI(title="Zabbix AI Dialog", lifespan=lifespan)


@app.exception_handler(AuthUnavailable)
async def _zabbix_unavailable(_request: Request, exc: AuthUnavailable) -> JSONResponse:
    """zabbix API 不可达：返回 503，避免误报为"未登录"。"""
    logger.warning("会话校验失败：%s", exc)
    return JSONResponse(
        {"error": "zabbix_unavailable", "detail": str(exc)}, status_code=503
    )


class ChatRequest(BaseModel):
    message: str
    clear: bool = False


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _get_ctx(request: Request) -> Dict[str, Any]:
    """校验会话，返回 {userid, username, roleid, sessionid}；失败抛 SessionError。"""
    return authenticate(dict(request.cookies), zclient, settings.auth_mode)


def _build_tool_ctx(user: Dict[str, Any]) -> ToolContext:
    return ToolContext(
        sessionid=user["sessionid"],
        username=user["username"] or user["userid"],
        zclient=zclient,
        suppliers=settings.suppliers,
        audit=storage.audit,
        message_template=settings.dingtalk_message_template,
    )


def _session_key(user: Dict[str, Any]) -> str:
    """对话隔离键 = Zabbix 登录会话 sessionid。

    这样"注销后重登"天然就是新会话（Zabbix 每次登录发新 sessionid，已实测），
    不依赖前端能否捕获注销动作；同一用户开两个浏览器也各自独立。
    """
    return str(user.get("sessionid") or user.get("userid") or user.get("username") or "")


def _history_ttl_seconds() -> int:
    return max(0, int(settings.history_ttl_minutes)) * 60


def _call_llm(user: Dict[str, Any], history_messages: List[Dict[str, Any]]) -> str:
    """把"已经存好的历史 + 本轮提问"交给模型。

    注意：调用方已把本轮用户消息写库，因此这里**不能再追加一次**——
    旧实现既写库又在历史里追加，同一句话会在上下文里出现两遍。
    """
    ctx = _build_tool_ctx(user)
    history: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[{"role": m["role"], "content": m["content"]} for m in history_messages],
    ]
    handlers = build_tool_handlers(ctx)
    return llm.chat(history, tools=build_tool_schemas(), function_handlers=handlers)


def _handle_slash_command(user: Dict[str, Any], text: str) -> str:
    """斜杠命令走确定性路径：由代码解析并执行，不依赖 LLM 是否决定调用工具。

    发消息到供应商钉钉群是有真实副作用的动作。若交给 LLM 自主决定，
    实测出现过"未调用工具却回答已发送成功"的编造行为，因此这里必须确定执行，
    保证"报告成功"等同于"确实发送成功"。
    """
    names = ", ".join(settings.supplier_names()) or "（未配置）"
    match = SLASH_RE.match(text)
    if not match:
        return (
            "斜杠命令格式：/供应商名 消息内容\n"
            f"可用供应商：{names}"
        )

    supplier_name = match.group(1)
    message = match.group(2).strip()
    ctx = _build_tool_ctx(user)
    result = tool_send_dingtalk(ctx, {"supplier": supplier_name, "message": message})

    if result.get("ok"):
        return (
            f"已发送到「{result['supplier']}」的钉钉群。\n"
            f"内容：{result['message']}\n"
            f"发送时间：{result['sent_at']}"
        )
    return f"发送失败：{result.get('error')}\n可用供应商：{names}"


@app.get("/")
def index() -> FileResponse:
    # 禁止缓存：升级插件后用户刷新即可拿到新前端，无需强刷
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/launcher.js")
def launcher() -> FileResponse:
    """漂浮聊天球脚本：由 nginx 注入到 Zabbix 页面（安装见 deploy/install_launcher.py）。

    单独一个路由而不是塞进 index.html：前端改版不会影响注入脚本，
    排查也方便——直接访问 /ai/launcher.js 就能看到内容。
    """
    return FileResponse(
        FRONTEND_DIR / "launcher.js",
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/license")
def license_notice():
    """许可证全文（GNU GPL v3.0），前端「关于」入口链接到这里。

    和 `/`、`/api/health` 一样**不需要登录**：法律声明应当可公开访问，
    且 GPL-3.0 §5(d) 要求有交互界面的作品展示 Appropriate Legal Notices。
    """
    if LICENSE_FILE.is_file():
        return FileResponse(
            LICENSE_FILE,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    # 兜底：正常不会发生（仓库与离线包都带 LICENSE），但也别给界面一个 404
    return JSONResponse(
        {
            "license": "GNU General Public License v3.0",
            "copyright": COPYRIGHT_NOTICE,
            "detail": "LICENSE 文件缺失，请见仓库根目录",
        }
    )


@app.get("/api/health")
def health() -> JSONResponse:
    """部署自检：未认证也可访问，故不返回供应商名单等业务信息。"""
    return JSONResponse(
        {
            "ok": True,
            "mysql": storage.ping(),
            "llm_model": settings.llm_model,
            "zabbix_api_configured": bool(settings.api_url),
        }
    )


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> JSONResponse:
    try:
        user = _get_ctx(request)
    except SessionError:
        return _unauthorized()

    user_key = _session_key(user)
    text = req.message.strip()
    context_reset = False

    try:
        if req.clear:
            storage.clear_messages(user_key)
            return JSONResponse({"reply": "会话已清空"})

        if not text:
            return JSONResponse({"reply": "请输入内容"})

        # 闲置超时 → 这轮算新会话（旧记录同时被删掉）
        ctx = storage.get_context(
            user_key, limit=settings.history_max_messages, ttl_seconds=_history_ttl_seconds()
        )
        context_reset = ctx["expired"]
        storage.add_message(user_key, "user", req.message)

        # 斜杠命令：确定性执行，避免模型编造"已发送"
        if text.startswith("/"):
            reply = _handle_slash_command(user, text)
        else:
            reply = _call_llm(
                user, [*ctx["messages"], {"role": "user", "content": req.message}]
            )
    except Unauthorized:
        return JSONResponse({"error": "zabbix_session_expired"}, status_code=401)
    except (LLMError, ZabbixError, StorageError) as exc:
        return JSONResponse({"reply": f"（服务异常）{exc}"}, status_code=502)

    try:
        storage.add_message(user_key, "assistant", reply)
    except StorageError as exc:
        logger.warning("保存回复失败: %s", exc)
    return JSONResponse({"reply": reply, "context_reset": context_reset})


@app.get("/api/history")
def history(request: Request) -> JSONResponse:
    try:
        user = _get_ctx(request)
    except SessionError:
        return _unauthorized()
    try:
        ctx = storage.get_context(
            _session_key(user),
            limit=settings.history_max_messages,
            ttl_seconds=_history_ttl_seconds(),
        )
    except StorageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    # suppliers 供前端在输入 / 时自动提示（此接口已鉴权，不对外泄露）
    # ttl_minutes 让前端能把"闲置多久算新会话"如实显示出来
    return JSONResponse(
        {
            "messages": ctx["messages"],
            "suppliers": settings.supplier_names(),
            "ttl_minutes": settings.history_ttl_minutes,
            "context_reset": ctx["expired"],
        }
    )


@app.get("/api/audit")
def audit(request: Request) -> JSONResponse:
    try:
        user = _get_ctx(request)
    except SessionError:
        return _unauthorized()
    # 仅超级管理员（roleid==3）可查审计；可按实际角色调整
    if str(user.get("roleid")) != "3":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        logs = storage.recent_audit()
    except StorageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"logs": logs})
