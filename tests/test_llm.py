"""LLM 抽象层测试：请求体组装、工具 schema 透传、工具调用循环（均不打网络）。"""
from __future__ import annotations

from app.llm import LLMClient, try_parse_tool_call

TOOLS = [
    {
        "name": "query_problems",
        "description": "查告警",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _client(**kwargs) -> LLMClient:
    params = dict(base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-4-flash", api_key="k")
    params.update(kwargs)
    return LLMClient(**params)


def test_endpoint_joins_base_url_without_double_slash():
    c = _client(base_url="https://open.bigmodel.cn/api/paas/v4/")
    assert c._endpoint() == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def test_bearer_header_uses_api_key():
    assert _client(api_key="sk-abc")._headers()["Authorization"] == "Bearer sk-abc"


def test_payload_contains_model_temperature_top_p_and_extra(monkeypatch):
    captured = {}
    c = _client(temperature=1.0, top_p=0.95, extra_params={"reasoning_effort": "max"})

    def fake_request(payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.chat([{"role": "user", "content": "hello"}])

    assert out == "hi"
    assert captured["model"] == "glm-4-flash"
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95
    assert captured["reasoning_effort"] == "max"


def test_extra_params_do_not_override_explicit_fields(monkeypatch):
    captured = {}
    c = _client(temperature=0.2, extra_params={"temperature": 9.9})

    def fake_request(payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(c, "_request", fake_request)
    c.chat([{"role": "user", "content": "x"}])
    assert captured["temperature"] == 0.2


def test_tools_forwarded_as_openai_function_schema(monkeypatch):
    captured = {}
    c = _client()

    def fake_request(payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(c, "_request", fake_request)
    c.chat([{"role": "user", "content": "x"}], tools=TOOLS)

    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "query_problems"


def test_no_tools_key_when_function_calling_disabled(monkeypatch):
    captured = {}
    c = _client(function_calling=False)

    def fake_request(payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(c, "_request", fake_request)
    c.chat([{"role": "user", "content": "x"}], tools=TOOLS)
    assert "tools" not in captured


def test_tool_call_loop_executes_handler_and_returns_final_text(monkeypatch):
    executed = []
    c = _client()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "query_problems",
                                    "arguments": '{"days": 3}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "最近有 2 条告警"}}]},
    ]

    def fake_request(payload):
        return responses.pop(0)

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.chat(
        [{"role": "user", "content": "最近告警?"}],
        tools=TOOLS,
        function_handlers={"query_problems": lambda a: executed.append(a) or {"count": 2}},
    )
    assert out == "最近有 2 条告警"
    assert executed == [{"days": 3}]


def test_tool_handler_exception_is_reported_back_to_model(monkeypatch):
    seen = {}
    c = _client()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "query_problems", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "工具失败了"}}]},
    ]

    def fake_request(payload):
        # 第二次请求里应带上工具错误内容
        if len(responses) == 1:
            seen["tool_msg"] = [
                m for m in payload["messages"] if m.get("role") == "tool"
            ][-1]["content"]
        return responses.pop(0)

    def boom(_args):
        raise RuntimeError("zabbix 不可用")

    monkeypatch.setattr(c, "_request", fake_request)
    out = c.chat(
        [{"role": "user", "content": "x"}],
        tools=TOOLS,
        function_handlers={"query_problems": boom},
    )
    assert out == "工具失败了"
    assert "zabbix 不可用" in seen["tool_msg"]


def test_unknown_tool_name_is_reported_back(monkeypatch):
    c = _client()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "nope", "arguments": "{}"}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "done"}}]},
    ]
    monkeypatch.setattr(c, "_request", lambda payload: responses.pop(0))
    out = c.chat(
        [{"role": "user", "content": "x"}], tools=TOOLS, function_handlers={}
    )
    assert out == "done"


def test_max_rounds_limit_returns_friendly_message(monkeypatch):
    c = _client()
    always_tool = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"id": "c", "function": {"name": "query_problems", "arguments": "{}"}}
                    ]
                }
            }
        ]
    }
    monkeypatch.setattr(c, "_request", lambda payload: always_tool)
    out = c.chat(
        [{"role": "user", "content": "x"}],
        tools=TOOLS,
        function_handlers={"query_problems": lambda a: {"ok": True}},
        max_rounds=2,
    )
    assert "上限" in out


def test_invalid_tool_arguments_json_falls_back_to_empty_dict(monkeypatch):
    seen = []
    c = _client()
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "query_problems", "arguments": "{bad"},}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "ok"}}]},
    ]
    monkeypatch.setattr(c, "_request", lambda payload: responses.pop(0))
    c.chat(
        [{"role": "user", "content": "x"}],
        tools=TOOLS,
        function_handlers={"query_problems": lambda a: seen.append(a) or {}},
    )
    assert seen == [{}]


# ---- 文本降级解析 ----

def test_parse_tool_call_extracts_name_and_arguments():
    parsed = try_parse_tool_call(
        'CALL:send_dingtalk|{"supplier": "网络供应商A", "message": "请排查"}'
    )
    assert parsed == {
        "name": "send_dingtalk",
        "arguments": {"supplier": "网络供应商A", "message": "请排查"},
    }


def test_parse_tool_call_returns_none_for_plain_text():
    assert try_parse_tool_call("最近没有告警") is None
