"""LLM 抽象层：OpenAI 兼容 /chat/completions。

对外暴露统一接口 chat(messages, tools) -> 文本。
- 模型支持 function calling：把 tools 以 functions 格式传给 API，处理 tool_calls 循环。
- 模型不支持：走降级路径（system 提示词附工具说明，解析模型输出的 JSON 指令）。

#5/#6 调研后切换后端，只需改 config.yaml 的 base_url / model / api_key。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

import httpx

# 每个工具：name / description / parameters(JSON Schema) / handler(可调用)
Tool = Dict[str, Any]


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        temperature: float = 0.3,
        top_p: float = 1.0,
        max_tokens: int = 1024,
        function_calling: bool = True,
        extra_params: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.function_calling = function_calling
        self.extra_params = dict(extra_params or {})
        self.timeout = timeout

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Tool]] = None,
        *,
        function_handlers: Optional[Dict[str, Callable]] = None,
        max_rounds: int = 5,
    ) -> str:
        """单轮用户消息驱动的完整对话：LLM 可能多次调用工具，最终返回文本。"""
        history = list(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": history,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        # 透传后端特有参数（如智谱 reasoning_effort），不覆盖上面显式设置的键
        for key, value in self.extra_params.items():
            payload.setdefault(key, value)

        use_tools = self.function_calling and tools

        if use_tools:
            # 组装 OpenAI function schema
            schema = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]
            payload["tools"] = [{"type": "function", "function": s} for s in schema]

        for _ in range(max_rounds):
            body = self._request(payload)
            msg = body["choices"][0]["message"]

            # 处理工具调用
            if use_tools and msg.get("tool_calls"):
                history.append(msg)
                for call in msg["tool_calls"]:
                    fn = call["function"]
                    name = fn["name"]
                    args_raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        args = {}
                    handler = (function_handlers or {}).get(name)
                    if handler:
                        try:
                            result = handler(args)
                            if not isinstance(result, str):
                                result = json.dumps(result, ensure_ascii=False)
                        except Exception as exc:  # 工具执行异常回传给 LLM
                            result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    else:
                        result = json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
                    history.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    })
                payload["messages"] = history
                continue

            content = msg.get("content") or ""
            # 非 function-calling 降级路径：模型可能直接给出文本回答
            if not use_tools:
                return content.strip()
            # function-calling 模式下若已到末轮且只有文本，直接返回
            return content.strip()

        # 达到工具调用轮次上限
        return "抱歉，本轮推理达到工具调用上限，请换种问法。"

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = httpx.post(self._endpoint(), headers=self._headers(), json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 连接失败: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()


# ---- 非 function-calling 的降级解析辅助 ----
def try_parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """从模型文本里提取工具调用指令（形如 CALL:name|{...json...}）。"""
    m = re.search(r"CALL:\s*([A-Za-z_]+)\s*\|?\s*(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        args = {}
    return {"name": m.group(1), "arguments": args}