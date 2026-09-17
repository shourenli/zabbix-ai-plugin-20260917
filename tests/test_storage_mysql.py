"""MySQL 存储集成测试。

默认跳过：未设置 TEST_MYSQL_HOST 时不执行，保证无 MySQL 环境下测试套件仍全绿。
需要真实 MySQL 8.0 时，先建库并授权（见 deploy/schema.sql），再运行：

    TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_USER=zabbix_ai \
    TEST_MYSQL_PASSWORD=xxx TEST_MYSQL_DB=zabbix_ai_plugin_test \
    python -m pytest tests/test_storage_mysql.py -v
"""
from __future__ import annotations

import os

import pytest

from app.storage import MySQLConfig, Storage, StorageError

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_MYSQL_HOST"),
    reason="未设置 TEST_MYSQL_HOST，跳过 MySQL 集成测试",
)


def _config() -> MySQLConfig:
    return MySQLConfig(
        host=os.environ["TEST_MYSQL_HOST"],
        port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
        database=os.environ.get("TEST_MYSQL_DB", "zabbix_ai_plugin_test"),
        user=os.environ.get("TEST_MYSQL_USER", "root"),
        password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
    )


@pytest.fixture()
def storage() -> Storage:
    st = Storage(_config())
    st.init_schema()
    return st


def test_init_schema_is_idempotent():
    st = Storage(_config())
    st.init_schema()
    st.init_schema()  # 再执行一次不应报错


def test_ping_succeeds_against_live_mysql(storage):
    assert storage.ping() is True


def test_messages_are_isolated_per_session(storage):
    """换一次登录即换会话键，彼此读不到——这正是"注销后重登看不到旧对话"。"""
    k1, k2 = "sess-test-1", "sess-test-2"
    storage.clear_messages(k1)
    storage.clear_messages(k2)

    storage.add_message(k1, "user", "hello")
    storage.add_message(k1, "assistant", "hi")
    storage.add_message(k2, "user", "other")

    assert [m["content"] for m in storage.get_messages(k1)] == ["hello", "hi"]
    assert [m["content"] for m in storage.get_messages(k2)] == ["other"]

    storage.clear_messages(k1)
    storage.clear_messages(k2)
    assert storage.get_messages(k1) == []


def test_idle_timeout_clears_conversation(storage):
    """闲置超过 TTL：下次取上下文应为空、旧记录已删、并标记 expired。"""
    import time as _t

    key = "sess-test-ttl"
    storage.clear_messages(key)
    storage.add_message(key, "user", "很久以前")
    storage._execute(
        "UPDATE sessions SET ts=%s WHERE user_id=%s", (int(_t.time()) - 7200, key)
    )

    ctx = storage.get_context(key, ttl_seconds=900)      # 15 分钟
    assert ctx["expired"] is True
    assert ctx["messages"] == []
    assert storage.get_messages(key) == [], "旧记录应已被删除"


def test_fresh_conversation_is_not_expired(storage):
    key = "sess-test-fresh"
    storage.clear_messages(key)
    storage.add_message(key, "user", "刚刚")
    ctx = storage.get_context(key, ttl_seconds=900)
    assert ctx["expired"] is False
    assert [m["content"] for m in ctx["messages"]] == ["刚刚"]
    storage.clear_messages(key)


def test_zero_ttl_never_expires(storage):
    import time as _t

    key = "sess-test-ttl0"
    storage.clear_messages(key)
    storage.add_message(key, "user", "很旧")
    storage._execute(
        "UPDATE sessions SET ts=%s WHERE user_id=%s", (int(_t.time()) - 7200, key)
    )
    ctx = storage.get_context(key, ttl_seconds=0)        # 0 = 不自动清
    assert ctx["expired"] is False
    assert [m["content"] for m in ctx["messages"]] == ["很旧"]
    storage.clear_messages(key)


def test_purge_expired_removes_old_rows(storage):
    import time as _t

    key = "sess-test-purge"
    storage.clear_messages(key)
    storage.add_message(key, "user", "陈年旧账")
    storage._execute(
        "UPDATE sessions SET ts=%s WHERE user_id=%s", (int(_t.time()) - 40 * 86400, key)
    )
    assert storage.purge_expired(30) >= 1
    assert storage.get_messages(key) == []
    assert storage.purge_expired(0) == 0, "0 = 不清理"


def test_history_order_is_chronological(storage):
    uid = "test-order"
    storage.clear_messages(uid)
    for i in range(3):
        storage.add_message(uid, "user", f"m{i}")
    assert [m["content"] for m in storage.get_messages(uid)] == ["m0", "m1", "m2"]
    storage.clear_messages(uid)


def test_history_respects_limit(storage):
    uid = "test-limit"
    storage.clear_messages(uid)
    for i in range(5):
        storage.add_message(uid, "user", f"m{i}")
    msgs = storage.get_messages(uid, limit=2)
    assert [m["content"] for m in msgs] == ["m3", "m4"]
    storage.clear_messages(uid)


def test_audit_round_trip(storage):
    storage.audit("tester", "send_dingtalk", "网络供应商A", "OK: 出口丢包")
    logs = storage.recent_audit(username="tester", limit=1)
    assert logs[0]["action"] == "send_dingtalk"
    assert logs[0]["target"] == "网络供应商A"
    assert "出口丢包" in logs[0]["detail"]


def test_clear_messages_returns_deleted_count(storage):
    uid = "test-count"
    storage.clear_messages(uid)
    storage.add_message(uid, "user", "a")
    storage.add_message(uid, "user", "b")
    assert storage.clear_messages(uid) == 2


def test_connection_failure_raises_storage_error():
    bad = Storage(
        MySQLConfig(
            host="127.0.0.1",
            port=1,
            database="nope",
            user="nope",
            password="nope",
            connect_timeout=1,
        )
    )
    with pytest.raises(StorageError):
        bad.init_schema()
    assert bad.ping() is False
