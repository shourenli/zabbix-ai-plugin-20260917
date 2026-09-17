-- ============================================================================
-- Zabbix AI 对话框插件 —— MySQL 8.0 初始化脚本
-- ============================================================================
-- 重要说明：
--   本插件【不读取 Zabbix 的数据库表】。所有监控数据都通过 Zabbix JSON-RPC API
--   获取（携带登录用户会话，权限天然隔离）。这里的库只用于存放插件自己的两张表：
--     sessions   会话历史（按 **Zabbix 登录会话** 隔离：换一次登录即新会话）
--     audit_log  审计日志（谁、何时、向哪个供应商发了什么）
--   因此请使用【独立数据库 + 独立最小权限账号】，不要复用 zabbix 库及其账号：
--     - 复用会让这个 Web 应用拿到整个 zabbix 库的读写权，权限过大
--     - 插件表混入 zabbix 库会污染其 schema，升级/还原时互相牵连
--
-- 执行顺序（先建库 → 再建账号授权 → 最后建表）：
--   1) 先把下面 'strong-password' 改成你自己的强密码
--   2) 在数据库服务器上执行：  sudo mysql < deploy/schema.sql
--   3) 把同一个密码写进插件目录的 .env：  MYSQL_PASSWORD=你的密码
--   4) 确认 config.yaml 的 storage.mysql.user 与这里创建的账号一致
-- ============================================================================

-- ---------- 1) 建库 ----------
CREATE DATABASE IF NOT EXISTS zabbix_ai_plugin
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

-- ---------- 2) 建账号（最小权限，仅本机访问）----------
-- 密码请自行替换；应用与数据库同机时用 127.0.0.1 即可
CREATE USER IF NOT EXISTS 'zabbix_ai'@'127.0.0.1' IDENTIFIED BY 'strong-password';

-- 【模式 A｜推荐】应用自动建表：config.yaml 保持 auto_init: true
-- 需要 CREATE / INDEX 权限，应用启动时会自动创建下面的两张表
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX
  ON zabbix_ai_plugin.* TO 'zabbix_ai'@'127.0.0.1';

-- 【模式 B｜DBA 预建表】config.yaml 设 auto_init: false
-- 表由本脚本预先建好，应用账号只给 DML 权限，不给任何 DDL 权限。
-- 启用模式 B 时：把上面那条 GRANT 注释掉，改用下面这条
-- GRANT SELECT, INSERT, UPDATE, DELETE
--   ON zabbix_ai_plugin.* TO 'zabbix_ai'@'127.0.0.1';

FLUSH PRIVILEGES;

-- ---------- 3) 建表（模式 B 必须执行；模式 A 执行也无妨，均为幂等）----------
USE zabbix_ai_plugin;

CREATE TABLE IF NOT EXISTS sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL COMMENT '会话隔离键 = zabbix sessionid（换一次登录即新会话）',
    role VARCHAR(16) NOT NULL COMMENT 'user / assistant / system',
    content MEDIUMTEXT NOT NULL,
    ts BIGINT NOT NULL COMMENT 'unix 秒',
    PRIMARY KEY (id),
    KEY idx_sessions_user_ts (user_id, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 关于 user_id 这一列（名字有历史包袱，说明一下）：
--   它现在存的是 **Zabbix 登录会话 sessionid**，不再存 userid。因为每次登录都会下发新的
--   sessionid，所以"注销后重登"天然就是新会话，看不到上一轮的对话。
--   为什么不新加一列：运行期账号按本脚本只有 SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX，
--   **没有 ALTER**；加列要 DBA 参与，而且改表失败容易被当成"服务照常启动"而漏掉。
--   复用既有列（VARCHAR(64) + (user_id, ts) 索引）意味着**升级到 v1.0.7 无需任何 DDL**。
--   升级后旧记录（键是数字 userid）在新规则下匹配不到，等于已清空，并由
--   history.retention_days 的兜底清理删除。

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- 4) 自检（可选）----------
-- 应能看到两张表：
-- SHOW TABLES;
-- 应能看到账号权限：
-- SHOW GRANTS FOR 'zabbix_ai'@'127.0.0.1';
