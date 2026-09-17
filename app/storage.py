"""MySQL 8.0 存储：会话历史（按**登录会话**隔离）+ 审计日志。

历史为什么按 sessionid 而不是 userid 隔离：按 userid 存的话，注销后重新登录
仍会看到上一轮的对话。而 Zabbix 每次登录都会下发新的 sessionid（实测确认），
所以绑定 sessionid 就等于"重登即新会话"，且注销后旧记录再也访问不到。

实现上**刻意不加新列**：会话键就存在原有的 `user_id` 列里（VARCHAR(64)，本来就有
`(user_id, ts)` 索引）。原因：运行期账号按 deploy/schema.sql 只被授予
SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX，**没有 ALTER**；加列必须 DBA 参与，
而且失败会被当成 warning 吞掉（实测过：迁移失败后服务照常启动，结果是每次对话 502，
问题被拖到用户侧才暴露）。复用既有列则新老安装都零 DDL、零权限要求，升级只换代码。
旧记录（键是数字 userid）在新规则下匹配不到，等于已经清空，并会被 retention 兜底删除。

运行环境为 Ubuntu 22.04/24.04 + MySQL 8.0（可与 zabbix 共用实例，使用独立库）；
表统一 InnoDB + utf8mb4。

连接策略：每次操作建立短连接（autocommit=True），不跨线程共享连接，
因此 FastAPI 的线程池并发下也安全，调用方无需管理连接生命周期。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger("zabbix_ai.storage")

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        user_id VARCHAR(64) NOT NULL COMMENT '会话隔离键 = zabbix sessionid（换一次登录即新会话）',
        role VARCHAR(16) NOT NULL COMMENT 'user / assistant / system',
        content MEDIUMTEXT NOT NULL,
        ts BIGINT NOT NULL COMMENT 'unix 秒',
        PRIMARY KEY (id),
        KEY idx_sessions_user_ts (user_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        username VARCHAR(128) NOT NULL,
        action VARCHAR(64) NOT NULL COMMENT '如 send_dingtalk',
        target VARCHAR(128) DEFAULT NULL COMMENT '供应商名等目标',
        detail TEXT COMMENT '发送内容摘要 / 错误信息',
        ts BIGINT NOT NULL COMMENT 'unix 秒',
        PRIMARY KEY (id),
        KEY idx_audit_ts (ts),
        KEY idx_audit_user_ts (username, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


class StorageError(Exception):
    """存储层错误（连接失败、SQL 失败等）。"""


class MySQLConfig:
    """MySQL 连接参数（密码由环境变量注入）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        database: str = "zabbix_ai_plugin",
        user: str = "zabbix_ai",
        password: str = "",
        charset: str = "utf8mb4",
        connect_timeout: int = 5,
    ):
        self.host = host
        self.port = int(port)
        self.database = database
        self.user = user
        self.password = password
        self.charset = charset
        self.connect_timeout = connect_timeout

    def describe(self) -> str:
        """用于日志展示，不含密码。"""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


class Storage:
    def __init__(self, config: MySQLConfig):
        self.config = config

    # ---- 连接 ----
    def _connect(self):
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset=self.config.charset,
                cursorclass=DictCursor,
                autocommit=True,
                connect_timeout=self.config.connect_timeout,
            )
        except pymysql.Error as exc:
            raise StorageError(f"MySQL 连接失败 ({self.config.describe()}): {exc}") from exc

    def init_schema(self) -> None:
        """建表（幂等）。数据库本身需预先创建，见 deploy/schema.sql。

        只做 CREATE TABLE IF NOT EXISTS，**不做任何 ALTER**：运行期账号按 schema.sql
        没有 ALTER 权限，而且"改表失败但服务照常启动"会把问题拖到用户侧才暴露
        （实测踩过：迁移失败被当 warning 吞掉，结果是每次对话 502）。
        """
        try:
            conn = self._connect()
        except StorageError:
            raise
        try:
            with conn.cursor() as cur:
                for stmt in SCHEMA_STATEMENTS:
                    cur.execute(stmt)
        except pymysql.Error as exc:
            raise StorageError(f"初始化表结构失败: {exc}") from exc
        finally:
            conn.close()

    def check_schema(self) -> Optional[str]:
        """只读自检：对话表是否真的可读写。返回错误说明（None = 正常）。

        启动时调用：宁可启动日志里留一条 ERROR，也不要等用户提问时才发现存储不可用。
        """
        try:
            conn = self._connect()
        except StorageError as exc:
            return str(exc)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, user_id, role, content, ts FROM sessions LIMIT 1")
                cur.fetchall()
            return None
        except pymysql.Error as exc:
            return "sessions 表不可用: %s" % exc
        finally:
            conn.close()

    def ping(self) -> bool:
        """连通性检查，供健康检查使用。"""
        try:
            conn = self._connect()
        except StorageError:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except pymysql.Error:
            return False
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple = (), *, fetch: Optional[str] = None):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "all":
                    return cur.fetchall()
                if fetch == "one":
                    return cur.fetchone()
                return cur.rowcount
        except pymysql.Error as exc:
            raise StorageError(f"SQL 执行失败: {exc}") from exc
        finally:
            conn.close()

    # ---- 会话历史（按登录会话隔离；键存在既有 user_id 列里，零 DDL）----
    def add_message(self, session_key: str, role: str, content: str) -> None:
        self._execute(
            "INSERT INTO sessions(user_id, role, content, ts) VALUES(%s, %s, %s, %s)",
            (session_key, role, content, int(time.time())),
        )

    def get_context(
        self, session_key: str, limit: int = 50, ttl_seconds: int = 0
    ) -> Dict[str, Any]:
        """取对话上下文，返回 {"messages": [...], "expired": bool}。

        ttl_seconds > 0 时：若最后一条消息距今已达到 ttl，说明这轮对话已经闲置过久，
        视为新会话——**顺手把旧记录删掉**并返回空上下文（expired=True），
        这样"服务端记得的"和用户以为的"新会话"始终一致。
        """
        rows = self._execute(
            "SELECT role, content, ts FROM sessions WHERE user_id=%s "
            "ORDER BY ts DESC, id DESC LIMIT %s",
            (session_key, int(limit)),
            fetch="all",
        )
        rows = list(rows or [])
        if not rows:
            return {"messages": [], "expired": False}

        if ttl_seconds > 0:
            newest = int(rows[0]["ts"])
            if int(time.time()) - newest >= int(ttl_seconds):
                removed = self.clear_messages(session_key)
                logger.info(
                    "对话闲置超过 %ds，已清空会话 %s... 的 %d 条记录",
                    int(ttl_seconds), session_key[:8], removed,
                )
                return {"messages": [], "expired": True}

        rows.reverse()
        return {
            "messages": [
                {"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows
            ],
            "expired": False,
        }

    def get_messages(
        self, session_key: str, limit: int = 50, ttl_seconds: int = 0
    ) -> List[Dict[str, Any]]:
        """只要消息列表的便捷方法（保留给旧调用与测试）。"""
        return self.get_context(session_key, limit=limit, ttl_seconds=ttl_seconds)["messages"]

    def clear_messages(self, session_key: str) -> int:
        return int(
            self._execute("DELETE FROM sessions WHERE user_id=%s", (session_key,)) or 0
        )

    def purge_expired(self, retention_days: int) -> int:
        """兜底清理：删除超过 retention_days 的历史记录（0 = 不清理）。"""
        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - int(retention_days) * 86400
        return int(self._execute("DELETE FROM sessions WHERE ts < %s", (cutoff,)) or 0)

    # ---- 审计 ----
    def audit(self, username: str, action: str, target: str, detail: str) -> None:
        self._execute(
            "INSERT INTO audit_log(username, action, target, detail, ts) VALUES(%s, %s, %s, %s, %s)",
            (username, action, target, detail, int(time.time())),
        )

    def recent_audit(self, username: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if username:
            rows = self._execute(
                "SELECT username, action, target, detail, ts FROM audit_log "
                "WHERE username=%s ORDER BY ts DESC, id DESC LIMIT %s",
                (username, int(limit)),
                fetch="all",
            )
        else:
            rows = self._execute(
                "SELECT username, action, target, detail, ts FROM audit_log "
                "ORDER BY ts DESC, id DESC LIMIT %s",
                (int(limit),),
                fetch="all",
            )
        return [
            {
                "username": r["username"],
                "action": r["action"],
                "target": r["target"],
                "detail": r["detail"],
                "ts": r["ts"],
            }
            for r in (rows or [])
        ]
