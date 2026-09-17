"""校验仓库自带的 config.yaml 关键约定，防止端口/模型被误改回归。"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"


def _shipped() -> Settings:
    return Settings(str(CONFIG_FILE))


def test_shipped_config_file_exists():
    assert CONFIG_FILE.exists()


def test_plugin_port_is_10801():
    """zabbix 前端占用 801，插件固定用 10801，不能用 8080。"""
    assert _shipped().port == 10801


def test_default_llm_is_tested_working_model():
    """glm-5.3-flash 在本账号下无可用资源包（429/1113），实测可用的是 glm-4-flash。"""
    s = _shipped()
    assert s.llm_model == "glm-4-flash"
    assert "open.bigmodel.cn" in s.llm_base_url


def test_function_calling_stays_enabled():
    assert _shipped().llm_function_calling is True


def test_mysql_targets_dedicated_schema_and_user():
    s = _shipped()
    assert s.mysql_database == "zabbix_ai_plugin"
    assert s.mysql_user == "zabbix_ai"


def test_auto_init_enabled_by_default():
    """默认走"应用自动建表"模式；改用 DBA 预建表时需显式改为 false。"""
    assert _shipped().mysql_auto_init is True


def test_auth_mode_passes_through_user_session():
    assert _shipped().auth_mode == "user"


def test_supplier_whitelist_is_not_empty():
    """白名单为空会导致 send_dingtalk 完全不可用，属部署配置错误。"""
    assert len(_shipped().supplier_names()) >= 1
