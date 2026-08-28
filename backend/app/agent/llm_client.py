from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI, OpenAI

from app.models.schemas import AgentSettings


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResult:
    content: str | None
    tool_calls: list[LLMToolCall]
    raw: Any | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class LLMStreamEvent:
    content_delta: str | None = None
    result: LLMResult | None = None

    @property
    def is_done(self) -> bool:
        return self.result is not None


class LLMClient:
    def __init__(self, api_key: str, base_url: str, *, max_content_chars: int = 40_000) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.max_content_chars = max_content_chars

    def generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        settings: AgentSettings,
    ) -> LLMResult:
        api_key = settings.api_key or self.api_key
        base_url = settings.api_base_url or self.base_url
        if not api_key:
            return self._mock_generate(messages)

        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=settings.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        choice = response.choices[0].message
        tool_calls: list[LLMToolCall] = []
        for item in choice.tool_calls or []:
            try:
                arguments = json.loads(item.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                LLMToolCall(
                    id=item.id,
                    name=item.function.name,
                    arguments=arguments,
                )
            )
        return LLMResult(content=self._bounded_text(choice.content), tool_calls=tool_calls, raw=response)

    async def stream_generate(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        settings: AgentSettings,
    ) -> AsyncIterator[LLMStreamEvent]:
        api_key = settings.api_key or self.api_key
        base_url = settings.api_base_url or self.base_url
        if not api_key:
            result = self._mock_generate(messages)
            if result.content:
                for index in range(0, len(result.content), 18):
                    yield LLMStreamEvent(content_delta=result.content[index : index + 18])
                    await asyncio.sleep(0)
            yield LLMStreamEvent(result=result)
            return

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        stream = None
        content_parts: list[str] = []
        content_chars = 0
        tool_call_parts: dict[int, dict[str, Any]] = {}
        try:
            stream = await client.chat.completions.create(
                model=settings.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    remaining = max(0, self.max_content_chars - content_chars)
                    visible = delta.content[:remaining]
                    if visible:
                        content_parts.append(visible)
                        content_chars += len(visible)
                        yield LLMStreamEvent(content_delta=visible)
                for item in delta.tool_calls or []:
                    slot = tool_call_parts.setdefault(
                        item.index,
                        {"id": "", "name": "", "arguments_parts": []},
                    )
                    if item.id:
                        slot["id"] = item.id
                    if item.function:
                        if item.function.name:
                            slot["name"] = item.function.name
                        if item.function.arguments:
                            slot["arguments_parts"].append(item.function.arguments)
        finally:
            if stream is not None:
                close_stream = getattr(stream, "close", None)
                if callable(close_stream):
                    closed = close_stream()
                    if inspect.isawaitable(closed):
                        await closed
            close_client = getattr(client, "close", None)
            if callable(close_client):
                closed = close_client()
                if inspect.isawaitable(closed):
                    await closed

        tool_calls: list[LLMToolCall] = []
        for index in sorted(tool_call_parts):
            item = tool_call_parts[index]
            name = item["name"]
            if not name:
                continue
            arguments_text = "".join(item["arguments_parts"])
            try:
                arguments = json.loads(arguments_text or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                LLMToolCall(
                    id=item["id"] or f"stream_call_{index}",
                    name=name,
                    arguments=arguments,
                )
            )
        yield LLMStreamEvent(result=LLMResult(content="".join(content_parts) or None, tool_calls=tool_calls))

    def _bounded_text(self, content: str | None) -> str | None:
        if content is None or len(content) <= self.max_content_chars:
            return content
        marker = "\n[模型输出已截断]"
        return content[: max(0, self.max_content_chars - len(marker))] + marker

    def _mock_generate(self, messages: list[dict[str, Any]]) -> LLMResult:
        last = messages[-1] if messages else {}
        if last.get("role") == "tool":
            try:
                payload = json.loads(last.get("content") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if payload.get("status") == "ok":
                result = payload.get("result", {})
                if isinstance(result, dict) and "matches" in result:
                    count = len(result.get("matches", []))
                    return LLMResult(content=f"我已经完成搜索，找到了 {count} 处匹配。", tool_calls=[])
                if isinstance(result, dict) and "entries" in result:
                    count = len(result.get("entries", []))
                    return LLMResult(content=f"我已经查看了目录，共列出 {count} 项。", tool_calls=[])
                if isinstance(result, dict) and "content" in result:
                    path = result.get("path", "")
                    return LLMResult(content=f"我已读取文件 {path}，可以继续分析。", tool_calls=[])
                if isinstance(result, dict) and "returncode" in result:
                    return LLMResult(content="命令已执行完毕，结果已返回。", tool_calls=[])
            return LLMResult(content="工具执行结束，但结果中包含错误。", tool_calls=[])

        user_text = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                user_text = item.get("content", "")
                break

        lowered = user_text.lower()
        if "todo" in lowered or "搜索" in user_text or "查找" in user_text:
            return LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="mock_call_1", name="search_text", arguments={"query": "TODO", "path": "."})],
            )
        if "结构" in user_text or "目录" in user_text:
            return LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="mock_call_1", name="list_files", arguments={"path": "."})],
            )
        return LLMResult(content="这是一个本地 mock Agent。当前没有配置 API Key，所以我返回了这条演示回复。", tool_calls=[])
