"""配置加载：读取 config.yaml + 环境变量。

密钥类信息（LLM api_key、MySQL 密码、钉钉 access_token/secret）通过环境变量注入，
不在 config.yaml / git 中明文保存。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("zabbix_ai.config")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_env_file(config_path: str) -> None:
    """加载 .env（不覆盖已存在的环境变量）。

    systemd 通过 EnvironmentFile 注入环境变量；但按文档手工执行 uvicorn 时不会加载，
    因此这里主动读取，保证两种启动方式行为一致。

    读取失败（例如 .env 权限为 600 而当前用户不是属主）只记警告、不抛异常：
    否则整个应用会直接起不来，且报错信息与"密钥没配好"难以区分。
    """
    candidates = [
        Path(config_path).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            load_dotenv(path, override=False)
        except Exception as exc:  # 权限不足 / 编码异常等
            logger.warning(
                "无法加载 %s（%s）；将只使用进程已有的环境变量", path, exc
            )


def _resolve_env(value: str) -> str:
    """把 '${VAR}' 形式的占位符替换为环境变量值；未定义则留空。"""

    def _sub(match: "re.Match[str]") -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_PATTERN.sub(_sub, value)


def _resolve(obj: Any) -> Any:
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v) for v in obj]
    return obj


DEFAULT_DINGTALK_TEMPLATE = "[zabbix-AI] {username} 上报：\n{message}"


class SupplierTarget:
    """一个供应商目标：钉钉群机器人 webhook + 可选安全设置 + 可选消息模板。"""

    def __init__(
        self,
        name: str,
        webhook: str,
        secret: str = "",
        keyword: str = "",
        message_template: str = "",
    ):
        self.name = name
        self.webhook = webhook
        self.secret = secret
        self.keyword = keyword
        # 为空表示沿用全局 dingtalk.message_template
        self.message_template = message_template

    def __repr__(self) -> str:  # 不暴露 token / secret
        return f"<SupplierTarget {self.name}>"


def _parse_suppliers(raw: Any) -> Dict[str, SupplierTarget]:
    """支持两种写法：

    suppliers:
      网络供应商A: "https://oapi.dingtalk.com/robot/send?access_token=xxx"     # 简写
      机房供应商B:                                                             # 完整
        webhook: "https://oapi.dingtalk.com/robot/send?access_token=yyy"
        secret_env: "DINGTALK_IDC_B_SECRET"    # 加签模式（环境变量引用）
        keyword: "告警"                          # 关键词模式
        message_template: "【告警】{message}"     # 可选：单独覆盖消息模板
    """
    out: Dict[str, SupplierTarget] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if isinstance(value, str):
            out[name] = SupplierTarget(name=name, webhook=value)
            continue
        if isinstance(value, dict):
            webhook = value.get("webhook") or value.get("url") or ""
            secret = value.get("secret") or ""
            secret_env = value.get("secret_env")
            if secret_env:
                secret = os.environ.get(secret_env, "")
            out[name] = SupplierTarget(
                name=name,
                webhook=webhook,
                secret=secret,
                keyword=value.get("keyword", "") or "",
                message_template=value.get("message_template", "") or "",
            )
            continue
        # 非法配置：保留条目但 webhook 为空，发送时会明确报错而非静默丢失
        out[name] = SupplierTarget(name=name, webhook="")
    return out


class Settings:
    def __init__(self, config_path: str = "config.yaml"):
        load_env_file(config_path)
        base_dir = Path(config_path).resolve().parent
        cfg: Dict[str, Any] = {}
        p = Path(config_path)
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = _resolve(cfg)

        self.zabbix = cfg.get("zabbix", {})
        self.llm = cfg.get("llm", {})
        self.dingtalk = cfg.get("dingtalk", {}) or {}
        self.suppliers: Dict[str, SupplierTarget] = _parse_suppliers(
            cfg.get("suppliers", {})
        )
        self.server = cfg.get("server", {})
        self.storage = cfg.get("storage", {})
        self.history = cfg.get("history", {}) or {}

        self.api_url: str = self.zabbix.get("api_url", "")
        self.auth_mode: str = (self.zabbix.get("auth", {}) or {}).get("mode", "user")

        # ---- 钉钉消息模板（全局默认；供应商级可覆盖）----
        # 可用占位符：{username} 发送人、{message} 消息正文
        self.dingtalk_message_template: str = (
            self.dingtalk.get("message_template") or DEFAULT_DINGTALK_TEMPLATE
        )

        # ---- LLM ----
        self.llm_base_url: str = self.llm.get("base_url", "")
        self.llm_model: str = self.llm.get("model", "")
        self.llm_api_key: str = os.environ.get(
            self.llm.get("api_key_env", "LLM_API_KEY"), ""
        )
        self.llm_temperature: float = float(self.llm.get("temperature", 0.3))
        self.llm_top_p: float = float(self.llm.get("top_p", 1.0))
        self.llm_max_tokens: int = int(self.llm.get("max_tokens", 1024))
        self.llm_function_calling: bool = bool(self.llm.get("function_calling", True))
        self.llm_extra_params: Dict[str, Any] = dict(
            self.llm.get("extra_params", {}) or {}
        )

        # ---- 服务 ----
        self.host: str = self.server.get("host", "127.0.0.1")
        self.port: int = int(self.server.get("port", 10801))
        self.base_path: str = self.server.get("base_path", "/ai")

        # ---- 存储：MySQL 8.0 ----
        mysql = self.storage.get("mysql", {}) or {}
        self.mysql_host: str = mysql.get("host", "127.0.0.1")
        self.mysql_port: int = int(mysql.get("port", 3306))
        self.mysql_database: str = mysql.get("database", "zabbix_ai_plugin")
        self.mysql_user: str = mysql.get("user", "zabbix_ai")
        self.mysql_password: str = os.environ.get(
            mysql.get("password_env", "MYSQL_PASSWORD"), ""
        )
        self.mysql_charset: str = mysql.get("charset", "utf8mb4")
        # true  = 应用启动时自动建表（账号需要 CREATE 权限）
        # false = 表由 DBA 依 deploy/schema.sql 预建，应用只需 DML 权限
        self.mysql_auto_init: bool = bool(mysql.get("auto_init", True))

        # ---- 对话历史（按登录会话隔离，见 storage.py）----
        # ttl_minutes     : 最后一条消息距今超过这么久 → 下次提问视为新会话（旧记录同时删除）；0=不自动清
        # max_messages    : 送入模型的历史条数上限
        # retention_days  : 兜底清理，启动时删除超过这么久的历史记录；0=不清理
        self.history_ttl_minutes: int = max(0, int(self.history.get("ttl_minutes", 15)))
        self.history_max_messages: int = max(1, int(self.history.get("max_messages", 50)))
        self.history_retention_days: int = max(0, int(self.history.get("retention_days", 7)))

    # ---- 供应商 ----
    def get_supplier(self, name: str) -> Optional[SupplierTarget]:
        """按名称查供应商；支持唯一前缀匹配；歧义或未注册返回 None。"""
        if name in self.suppliers:
            return self.suppliers[name]
        hits = [t for t in self.suppliers.values() if t.name.startswith(name)]
        if len(hits) == 1:
            return hits[0]
        return None

    def supplier_names(self) -> List[str]:
        return list(self.suppliers.keys())


# 模块级单例：应用启动与路由复用；测试可自行 Settings(路径) 构造独立实例。
settings = Settings()
