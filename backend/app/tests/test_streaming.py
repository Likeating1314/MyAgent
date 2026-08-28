from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agent.controller import AgentController
from app.agent.llm_client import LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.tool_executor import ToolContext
from app.agent.tool_registry import ToolDefinition, ToolRegistry
from app.config import AppSettings
from app.models.schemas import ChatRequest
from app.services.session_store import InMemorySessionStore


class ProbeArgs(BaseModel):
    value: str


class ScriptedAsyncLLM:
    def __init__(self, scripts: list[list[LLMStreamEvent] | Exception]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream_generate(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        del kwargs
        script = self.scripts[self.calls]
        self.calls += 1
        if isinstance(script, Exception):
            raise script
        for event in script:
            yield event
            await asyncio.sleep(0)


class SlowAsyncLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream_generate(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        del kwargs
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield LLMStreamEvent(result=LLMResult(content="unreachable", tool_calls=[]))
        finally:
            self.closed.set()


def app_settings(tmp_path: Path, *, max_steps: int = 4) -> AppSettings:
    return AppSettings(
        OPENAI_API_KEY="",
        OPENAI_BASE_URL="https://api.openai.com/v1",
        OPENAI_MODEL="gpt-4.1-mini",
        WORKSPACE_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        ALLOW_COMMAND_EXECUTION=False,
        MAX_AGENT_STEPS=max_steps,
    )


def parse_sse(raw_events: list[str]) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for raw in raw_events:
        event_name = "message"
        data_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        parsed.append((event_name, json.loads("\n".join(data_lines))))
    return parsed


def collect_stream(
    controller: AgentController,
    *,
    session_id: str = "stream-session",
    is_cancelled=None,  # noqa: ANN001
) -> list[tuple[str, dict[str, Any]]]:
    async def collect() -> list[str]:
        request = ChatRequest(session_id=session_id, message="run")
        return [
            event
            async for event in controller.handle_chat_stream(
                request,
                is_cancelled=is_cancelled,
            )
        ]

    return parse_sse(asyncio.run(collect()))


def terminal_names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events if name in {"done", "error", "cancelled"}]


def test_normal_stream_emits_delta_then_done(tmp_path: Path) -> None:
    llm = ScriptedAsyncLLM(
        [[LLMStreamEvent(content_delta="hel"), LLMStreamEvent(content_delta="lo"),
          LLMStreamEvent(result=LLMResult(content="hello", tool_calls=[]))]]
    )
    controller = AgentController(app_settings=app_settings(tmp_path), llm_client=llm)

    events = collect_stream(controller)

    assert [name for name, _ in events] == ["delta", "delta", "done"]
    assert "".join(payload["content"] for name, payload in events if name == "delta") == "hello"
    assert terminal_names(events) == ["done"]


def test_model_exception_emits_safe_error_without_done(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    llm = ScriptedAsyncLLM([RuntimeError("secret model response")])
    controller = AgentController(
        app_settings=app_settings(tmp_path), session_store=store, llm_client=llm
    )

    events = collect_stream(controller)

    assert terminal_names(events) == ["error"]
    assert events[-1][1]["code"] == "model_error"
    assert "secret" not in json.dumps(events, ensure_ascii=False)
    session = store.get("stream-session")
    assert session is not None
    assert [message["role"] for message in session.messages] == ["user"]
    assert session.messages[0]["content"] == "run"


def test_user_cancellation_emits_cancelled_and_saves_no_empty_assistant(tmp_path: Path) -> None:
    cancelled = {"value": False}
    store = InMemorySessionStore()

    class CancelAfterDeltaLLM:
        async def stream_generate(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
            del kwargs
            yield LLMStreamEvent(content_delta="partial")
            cancelled["value"] = True
            await asyncio.sleep(0)
            yield LLMStreamEvent(result=LLMResult(content="partial", tool_calls=[]))

    async def is_cancelled() -> bool:
        return cancelled["value"]

    controller = AgentController(
        app_settings=app_settings(tmp_path),
        session_store=store,
        llm_client=CancelAfterDeltaLLM(),
    )

    events = collect_stream(controller, is_cancelled=is_cancelled)

    assert [name for name, _ in events][0] == "delta"
    assert terminal_names(events) == ["cancelled"]
    session = store.get("stream-session")
    assert session is not None
    assert session.messages == [{"role": "user", "content": "run"}]


def test_cancellation_after_tool_prevents_later_tools_and_model_rounds(tmp_path: Path) -> None:
    cancelled = {"value": False}
    executed: list[str] = []

    def probe(context: ToolContext, args: ProbeArgs) -> dict[str, str]:
        del context
        executed.append(args.value)
        cancelled["value"] = True
        return {"value": args.value}

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", ProbeArgs, probe))
    first_result = LLMResult(
        content=None,
        tool_calls=[
            LLMToolCall(id="call-1", name="probe", arguments={"value": "first"}),
            LLMToolCall(id="call-2", name="probe", arguments={"value": "second"}),
        ],
    )
    llm = ScriptedAsyncLLM(
        [[LLMStreamEvent(result=first_result)],
         [LLMStreamEvent(result=LLMResult(content="should not run", tool_calls=[]))]]
    )

    async def is_cancelled() -> bool:
        return cancelled["value"]

    store = InMemorySessionStore()
    controller = AgentController(
        app_settings=app_settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=llm,
    )

    events = collect_stream(controller, is_cancelled=is_cancelled)

    assert executed == ["first"]
    assert llm.calls == 1
    assert terminal_names(events) == ["cancelled"]
    session = store.get("stream-session")
    assert session is not None
    assert len(session.tool_calls) == 1
    assert all(message.get("content") != "" for message in session.messages)


def test_disconnect_closes_model_stream_and_stops_processing(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], SlowAsyncLLM, AgentController]:
        disconnected = {"value": False}
        llm = SlowAsyncLLM()
        controller = AgentController(app_settings=app_settings(tmp_path), llm_client=llm)

        async def is_disconnected() -> bool:
            return disconnected["value"]

        async def disconnect() -> None:
            await llm.started.wait()
            disconnected["value"] = True

        disconnect_task = asyncio.create_task(disconnect())
        raw_events = [
            event
            async for event in controller.handle_chat_stream(
                ChatRequest(session_id="disconnect", message="run"),
                is_cancelled=is_disconnected,
            )
        ]
        await disconnect_task
        await asyncio.wait_for(llm.closed.wait(), timeout=1)
        return raw_events, llm, controller

    raw_events, llm, controller = asyncio.run(scenario())
    events = parse_sse(raw_events)

    assert terminal_names(events) == ["cancelled"]
    assert llm.calls == 1
    run_id = controller.session_store.acquire_run("disconnect", lease_seconds=5)
    assert controller.session_store.release_run("disconnect", run_id) is True


def test_tool_runs_off_event_loop_thread(tmp_path: Path) -> None:
    thread_ids: list[int] = []

    def probe(context: ToolContext, args: ProbeArgs) -> dict[str, str]:
        del context
        thread_ids.append(threading.get_ident())
        return {"value": args.value}

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", ProbeArgs, probe))
    llm = ScriptedAsyncLLM(
        [
            [LLMStreamEvent(result=LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="call-1", name="probe", arguments={"value": "ok"})],
            ))],
            [LLMStreamEvent(result=LLMResult(content="done", tool_calls=[]))],
        ]
    )
    controller = AgentController(
        app_settings=app_settings(tmp_path), registry=registry, llm_client=llm
    )

    async def scenario() -> tuple[int, list[str]]:
        loop_thread = threading.get_ident()
        raw = [
            event
            async for event in controller.handle_chat_stream(
                ChatRequest(session_id="thread", message="run")
            )
        ]
        return loop_thread, raw

    loop_thread, raw_events = asyncio.run(scenario())

    assert thread_ids and thread_ids[0] != loop_thread
    assert terminal_names(parse_sse(raw_events)) == ["done"]


def test_completed_tool_record_survives_later_model_failure(tmp_path: Path) -> None:
    def probe(context: ToolContext, args: ProbeArgs) -> dict[str, str]:
        del context
        return {"value": args.value}

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", ProbeArgs, probe))
    llm = ScriptedAsyncLLM(
        [
            [LLMStreamEvent(result=LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="call-1", name="probe", arguments={"value": "saved"})],
            ))],
            RuntimeError("later model failure"),
        ]
    )
    store = InMemorySessionStore()
    controller = AgentController(
        app_settings=app_settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=llm,
    )

    events = collect_stream(controller)

    assert [name for name, _ in events] == ["tool_call", "error"]
    assert terminal_names(events) == ["error"]
    session = store.get("stream-session")
    assert session is not None
    assert len(session.tool_calls) == 1
    assert session.tool_calls[0].result == {"value": "saved"}
    assert [message["role"] for message in session.messages] == ["user", "assistant", "tool"]
    assert all(message.get("content") != "" for message in session.messages)
