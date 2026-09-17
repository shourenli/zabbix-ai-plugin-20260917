"""配置加载测试：MySQL 存储配置、GLM 参数、环境变量注入、供应商白名单与安全设置。"""
from __future__ import annotations

import os

from app.config import Settings

BASE_CONFIG = """
zabbix:
  frontend_url: "https://zbx.example.com"
  api_url: "http://zbx.example.com/api_jsonrpc.php"
  auth:
    mode: "user"

llm:
  base_url: "https://open.bigmodel.cn/api/paas/v4"
  model: "glm-4-flash"
  api_key_env: "TEST_LLM_KEY"
  temperature: 0.3
  top_p: 0.9
  max_tokens: 1024
  function_calling: true
  extra_params:
    reasoning_effort: "max"

suppliers:
  网络供应商A:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${TEST_DT_A}"
    keyword: "告警"
  机房供应商B:
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=${TEST_DT_B}"
    secret_env: "TEST_DT_B_SECRET"

server:
  host: "127.0.0.1"
  port: 10801
  base_path: "/ai"

storage:
  mysql:
    host: "db.internal"
    port: 3307
    database: "zabbix_ai_plugin"
    user: "zabbix_ai"
    password_env: "TEST_MYSQL_PW"
    charset: "utf8mb4"
"""


def _settings(tmp_path) -> Settings:
    p = tmp_path / "config.yaml"
    p.write_text(BASE_CONFIG, encoding="utf-8")
    return Settings(str(p))


# ---- .env 加载健壮性 ----

def test_env_load_failure_does_not_break_startup(tmp_path, monkeypatch):
    """回归：.env 存在但读不到（如权限 600 且非属主）时，不能让应用起不来。

    生产上服务以 owner 运行不受影响，但换用户手工启动或权限被改时会撞到；
    此时应降级为"只用进程环境变量"并记警告，而不是抛 PermissionError。
    """
    (tmp_path / ".env").write_text("LLM_API_KEY=from_env_file\n", encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(BASE_CONFIG, encoding="utf-8")

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.config.load_dotenv", boom)
    s = Settings(str(p))          # 不应抛异常
    assert s.llm_model == "glm-4-flash"


def test_env_file_values_are_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TEST_LLM_KEY=from_env_file\n", encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    assert Settings(str(p)).llm_api_key == "from_env_file"


# ---- MySQL ----

def test_loads_mysql_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MYSQL_PW", "s3cret")
    s = _settings(tmp_path)
    assert s.mysql_host == "db.internal"
    assert s.mysql_port == 3307
    assert s.mysql_database == "zabbix_ai_plugin"
    assert s.mysql_user == "zabbix_ai"
    assert s.mysql_password == "s3cret"
    assert s.mysql_charset == "utf8mb4"


def test_no_sqlite_config_remains(tmp_path):
    """确认已彻底移除 SQLite 配置项。"""
    assert not hasattr(_settings(tmp_path), "db_path")


def test_mysql_auto_init_defaults_true(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text('storage:\n  mysql:\n    host: "h"\n', encoding="utf-8")
    assert Settings(str(p)).mysql_auto_init is True


def test_mysql_auto_init_can_be_disabled_for_dba_managed_schema(tmp_path):
    """DBA 预建表、应用仅具 DML 权限时应关闭自动建表。"""
    p = tmp_path / "config.yaml"
    p.write_text("storage:\n  mysql:\n    auto_init: false\n", encoding="utf-8")
    assert Settings(str(p)).mysql_auto_init is False


# ---- 服务 ----

def test_server_settings(tmp_path):
    s = _settings(tmp_path)
    assert s.port == 10801
    assert s.host == "127.0.0.1"
    assert s.base_path == "/ai"


# ---- LLM ----

def test_llm_settings_including_glm_params(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    s = _settings(tmp_path)
    assert s.llm_model == "glm-4-flash"
    assert s.llm_base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert s.llm_api_key == "sk-test"
    assert s.llm_temperature == 0.3
    assert s.llm_top_p == 0.9
    assert s.llm_extra_params == {"reasoning_effort": "max"}
    assert s.llm_function_calling is True


def test_missing_api_key_yields_empty_string(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    assert _settings(tmp_path).llm_api_key == ""


# ---- 供应商：环境变量注入 ----

def test_env_placeholder_resolved_in_suppliers(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DT_A", "tokenA")
    monkeypatch.setenv("TEST_DT_B", "tokenB")
    s = _settings(tmp_path)
    assert s.suppliers["网络供应商A"].webhook.endswith("access_token=tokenA")
    assert s.suppliers["机房供应商B"].webhook.endswith("access_token=tokenB")


def test_undefined_env_placeholder_becomes_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DT_A", raising=False)
    s = _settings(tmp_path)
    assert s.suppliers["网络供应商A"].webhook.endswith("access_token=")


# ---- 供应商：安全设置 ----

def test_keyword_mode_is_parsed(tmp_path):
    s = _settings(tmp_path)
    assert s.suppliers["网络供应商A"].keyword == "告警"
    assert s.suppliers["网络供应商A"].secret == ""


def test_sign_mode_secret_resolved_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DT_B_SECRET", "SECxyz")
    s = _settings(tmp_path)
    assert s.suppliers["机房供应商B"].secret == "SECxyz"


def test_sign_mode_secret_empty_when_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DT_B_SECRET", raising=False)
    assert _settings(tmp_path).suppliers["机房供应商B"].secret == ""


def test_plain_string_supplier_form_still_supported(tmp_path):
    """兼容简写形式：直接给 URL 字符串。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        'suppliers:\n  简单供应商: "https://hook/x?access_token=T"\n',
        encoding="utf-8",
    )
    s = Settings(str(p))
    target = s.suppliers["简单供应商"]
    assert target.webhook == "https://hook/x?access_token=T"
    assert target.secret == ""
    assert target.keyword == ""


def test_invalid_supplier_value_keeps_entry_with_empty_webhook(tmp_path):
    """非法配置不应静默丢条目，发送时会明确报错。"""
    p = tmp_path / "config.yaml"
    p.write_text("suppliers:\n  坏供应商: 12345\n", encoding="utf-8")
    s = Settings(str(p))
    assert s.suppliers["坏供应商"].webhook == ""


def test_supplier_repr_does_not_leak_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DT_B_SECRET", "SUPERSECRET")
    s = _settings(tmp_path)
    assert "SUPERSECRET" not in repr(s.suppliers["机房供应商B"])


# ---- 供应商：解析（精确/前缀/歧义）----

def test_get_supplier_exact_prefix_and_unknown(tmp_path):
    s = _settings(tmp_path)
    assert s.get_supplier("网络供应商A").name == "网络供应商A"
    assert s.get_supplier("网络").name == "网络供应商A"   # 唯一前缀
    assert s.get_supplier("机房").name == "机房供应商B"
    assert s.get_supplier("不存在") is None


def test_get_supplier_ambiguous_prefix_returns_none(tmp_path):
    """前缀命中多个时应返回 None，避免误发给错误供应商。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        'suppliers:\n  网络供应商A: "https://hook/a"\n  网络供应商B: "https://hook/b"\n',
        encoding="utf-8",
    )
    s = Settings(str(p))
    assert s.get_supplier("网络") is None
    assert s.get_supplier("网络供应商A").webhook == "https://hook/a"


# ---- 钉钉消息模板 ----

def test_dingtalk_template_uses_builtin_default_when_absent(tmp_path):
    from app.config import DEFAULT_DINGTALK_TEMPLATE

    p = tmp_path / "config.yaml"
    p.write_text("suppliers: {}\n", encoding="utf-8")
    assert Settings(str(p)).dingtalk_message_template == DEFAULT_DINGTALK_TEMPLATE


def test_dingtalk_template_can_be_overridden_globally(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        'dingtalk:\n  message_template: "【告警】{message}"\n', encoding="utf-8"
    )
    assert Settings(str(p)).dingtalk_message_template == "【告警】{message}"


def test_supplier_level_template_is_parsed(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "suppliers:\n"
        "  供应商X:\n"
        '    webhook: "https://hook/x"\n'
        '    message_template: "【来自监控】{message}"\n'
        "  供应商Y:\n"
        '    webhook: "https://hook/y"\n',
        encoding="utf-8",
    )
    s = Settings(str(p))
    assert s.suppliers["供应商X"].message_template == "【来自监控】{message}"
    # 未配置的供应商为空字符串，表示沿用全局模板
    assert s.suppliers["供应商Y"].message_template == ""



def test_missing_config_file_uses_defaults(tmp_path):
    s = Settings(str(tmp_path / "absent.yaml"))
    assert s.mysql_database == "zabbix_ai_plugin"
    assert s.mysql_port == 3306
    assert s.port == 10801
    assert s.supplier_names() == []
    assert s.llm_api_key == os.environ.get("LLM_API_KEY", "")


# ---- 对话历史参数（v1.0.7）----

def test_history_defaults_to_15_minute_idle_timeout(tmp_path):
    s = _settings(tmp_path)          # BASE_CONFIG 里没有 history 段
    assert s.history_ttl_minutes == 15
    assert s.history_max_messages == 50
    assert s.history_retention_days == 7


def test_history_can_be_configured(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "history:\n  ttl_minutes: 30\n  max_messages: 10\n  retention_days: 0\n",
        encoding="utf-8",
    )
    s = Settings(str(p))
    assert s.history_ttl_minutes == 30
    assert s.history_max_messages == 10
    assert s.history_retention_days == 0      # 0 = 不清理


def test_history_zero_ttl_disables_auto_clear(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("history:\n  ttl_minutes: 0\n", encoding="utf-8")
    assert Settings(str(p)).history_ttl_minutes == 0


def test_history_negative_values_are_clamped(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("history:\n  ttl_minutes: -5\n  max_messages: 0\n", encoding="utf-8")
    s = Settings(str(p))
    assert s.history_ttl_minutes == 0
    assert s.history_max_messages == 1        # 至少留一条，否则模型没有上下文
