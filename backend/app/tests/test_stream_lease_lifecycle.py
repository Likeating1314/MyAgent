from __future__ import annotations

import asyncio
import gc
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.agent.controller import AgentController
from app.agent.llm_client import LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.tool_executor import ToolContext
from app.agent.tool_registry import ToolDefinition, ToolRegistry
from app.api import routes
from app.api.routes import (
    _acquire_stream_run,
    _session_busy_http_error,
    chat_stream,
    resume_approval_stream,
)
from app.config import AppSettings
from app.models.schemas import ApprovalResumeRequest, ChatRequest
from app.services.approval_store import ApprovalStore
from app.services.session_store import InMemorySessionStore, SessionBusyError
from app.services.stream_lease import StreamRunLeaseGuard
from pydantic import BaseModel


def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        OPENAI_API_KEY="",
        OPENAI_BASE_URL="https://api.openai.com/v1",
        OPENAI_MODEL="gpt-4.1-mini",
        WORKSPACE_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        ALLOW_COMMAND_EXECUTION=False,
        SESSION_RUN_LEASE_SECONDS=120,
    )


def request_for(
    controller: AgentController, approval_store: ApprovalStore | None = None
) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            controller=controller,
            session_store=controller.session_store,
            approval_store=approval_store,
        )
    )
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": True}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "app": app},
        receive=receive,
    )


def parse_event(raw: str) -> tuple[str, dict]:
    name = next(line[6:].strip() for line in raw.splitlines() if line.startswith("event:"))
    data = "\n".join(line[5:].lstrip() for line in raw.splitlines() if line.startswith("data:"))
    return name, json.loads(data)


def approved_list_files(store: ApprovalStore, session_id: str):  # noqa: ANN201
    approval = store.create_pending(
        session_id=session_id,
        tool_name="list_files",
        arguments={"path": ".", "max_depth": 1},
        reason="list files",
    )
    return store.set_status(approval.id, "approved")


def test_session_busy_error_has_stable_cleanup_message() -> None:
    error = _session_busy_http_error(SessionBusyError("internal store wording"))
    assert error.status_code == 409
    assert error.detail == {
        "code": "session_busy",
        "message": "上一任务仍在完成已启动的工具，请稍后重试。",
    }


def test_cancel_during_run_acquisition_releases_late_acquired_lease(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAcquireStore(InMemorySessionStore):
        def acquire_run(self, session_id: str, *, lease_seconds: int) -> str:
            started.set()
            assert release.wait(timeout=3)
            return super().acquire_run(session_id, lease_seconds=lease_seconds)

    async def scenario() -> None:
        store = BlockingAcquireStore()
        controller = AgentController(app_settings=settings(tmp_path), session_store=store)
        task = asyncio.create_task(_acquire_stream_run(controller, "acquire-cancel"))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        replacement = controller.acquire_session_run("acquire-cancel")
        assert store.release_run("acquire-cancel", replacement) is True

    asyncio.run(scenario())


def test_chat_route_releases_run_when_response_body_never_starts(tmp_path: Path) -> None:
    store = InMemorySessionStore()
    controller = AgentController(app_settings=settings(tmp_path), session_store=store)

    async def scenario() -> None:
        response = await chat_stream(
            request_for(controller),
            ChatRequest(session_id="never-started", message="search files"),
        )
        del response
        gc.collect()
        replacement = controller.acquire_session_run("never-started")
        assert store.release_run("never-started", replacement) is True

    asyncio.run(scenario())


def test_approval_route_releases_run_when_response_body_never_starts(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    approval_store = ApprovalStore(app_settings.resolved_sqlite_path())
    store = InMemorySessionStore()
    store.get_or_create("approval-never-started")
    approval = approved_list_files(approval_store, "approval-never-started")
    controller = AgentController(
        app_settings=app_settings,
        session_store=store,
        approval_store=approval_store,
    )

    async def scenario() -> None:
        response = await resume_approval_stream(
            request_for(controller, approval_store),
            approval.id,
            ApprovalResumeRequest(),
        )
        del response
        gc.collect()
        replacement = controller.acquire_session_run("approval-never-started")
        assert store.release_run("approval-never-started", replacement) is True

    asyncio.run(scenario())


@pytest.mark.parametrize("approval", [False, True])
def test_response_construction_failure_releases_acquired_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, approval: bool
) -> None:
    app_settings = settings(tmp_path)
    store = InMemorySessionStore()
    approval_store = ApprovalStore(app_settings.resolved_sqlite_path())
    session_id = "approval-construction" if approval else "chat-construction"
    store.get_or_create(session_id)
    controller = AgentController(
        app_settings=app_settings,
        session_store=store,
        approval_store=approval_store,
    )
    approved = approved_list_files(approval_store, session_id) if approval else None

    class FailingResponse:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            del args, kwargs
            raise RuntimeError("response construction failed")

    monkeypatch.setattr(routes, "LeaseStreamingResponse", FailingResponse)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="construction failed"):
            if approved is not None:
                await resume_approval_stream(
                    request_for(controller, approval_store),
                    approved.id,
                    ApprovalResumeRequest(),
                )
            else:
                await chat_stream(
                    request_for(controller),
                    ChatRequest(session_id=session_id, message="hello"),
                )
        replacement = controller.acquire_session_run(session_id)
        assert store.release_run(session_id, replacement) is True

    asyncio.run(scenario())


def test_first_iteration_cancel_and_aclose_release_chat_run(tmp_path: Path) -> None:
    class WaitingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def stream_generate(self, **kwargs):  # noqa: ANN001, ANN201
            del kwargs
            self.started.set()
            await asyncio.Event().wait()
            yield LLMStreamEvent(result=LLMResult(content="unreachable", tool_calls=[]))

    async def scenario() -> None:
        store = InMemorySessionStore()
        llm = WaitingLLM()
        controller = AgentController(
            app_settings=settings(tmp_path), session_store=store, llm_client=llm
        )
        response = await chat_stream(
            request_for(controller),
            ChatRequest(session_id="cancel-first", message="hello"),
        )
        first = asyncio.create_task(anext(response.body_iterator))
        await asyncio.wait_for(llm.started.wait(), timeout=3)
        first.cancel()
        with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
            await asyncio.wait_for(first, timeout=3)
        await asyncio.wait_for(response.body_iterator.aclose(), timeout=3)
        replacement = controller.acquire_session_run("cancel-first")
        assert store.release_run("cancel-first", replacement) is True

    asyncio.run(scenario())


def test_partial_mock_stream_aclose_releases_run_without_waiting_for_expiry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = InMemorySessionStore()
        controller = AgentController(app_settings=settings(tmp_path), session_store=store)
        response = await chat_stream(
            request_for(controller),
            ChatRequest(
                session_id="mock-close",
                message="搜索 TODO",
                settings={"api_key": ""},
            ),
        )
        name, _ = parse_event(await anext(response.body_iterator))
        assert name == "tool_call"
        await response.body_iterator.aclose()
        replacement = controller.acquire_session_run("mock-close")
        assert store.release_run("mock-close", replacement) is True
        session = store.get("mock-close")
        assert session is not None
        assert len(session.tool_calls) == 1

    asyncio.run(scenario())


def test_asgi_disconnect_releases_chat_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = InMemorySessionStore()
        controller = AgentController(app_settings=settings(tmp_path), session_store=store)
        response = await chat_stream(
            request_for(controller),
            ChatRequest(session_id="asgi-disconnect", message="hello"),
        )
        receive_calls = 0

        async def receive() -> dict:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": b"", "more_body": True}
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            del message

        await response(
            {"type": "http", "method": "POST", "path": "/api/chat/stream"},
            receive,
            send,
        )
        replacement = controller.acquire_session_run("asgi-disconnect")
        assert store.release_run("asgi-disconnect", replacement) is True

    asyncio.run(scenario())


def test_approval_route_partial_read_then_aclose_releases_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        app_settings = settings(tmp_path)
        approval_store = ApprovalStore(app_settings.resolved_sqlite_path())
        store = InMemorySessionStore()
        store.get_or_create("approval-close")
        store.append_event(
            "approval-close", "message", {"role": "user", "content": "list files"}
        )
        approval = approved_list_files(approval_store, "approval-close")
        controller = AgentController(
            app_settings=app_settings,
            session_store=store,
            approval_store=approval_store,
        )
        response = await resume_approval_stream(
            request_for(controller, approval_store),
            approval.id,
            ApprovalResumeRequest(),
        )
        name, _ = parse_event(await anext(response.body_iterator))
        assert name == "tool_call"
        await response.body_iterator.aclose()
        replacement = controller.acquire_session_run("approval-close")
        assert store.release_run("approval-close", replacement) is True
        session = store.get("approval-close")
        assert session is not None and len(session.tool_calls) == 1

    asyncio.run(scenario())


class BlockingArgs(BaseModel):
    value: str


def test_cancel_during_sync_tool_stays_busy_only_until_tool_cleanup(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    executed: list[str] = []

    def blocking_tool(context: ToolContext, args: BlockingArgs) -> dict[str, str]:
        del context
        started.set()
        assert release.wait(timeout=3)
        executed.append(args.value)
        return {"value": args.value}

    class ToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_generate(self, **kwargs) -> AsyncIterator[LLMStreamEvent]:  # noqa: ANN001
            del kwargs
            self.calls += 1
            yield LLMStreamEvent(
                result=LLMResult(
                    content=None,
                    tool_calls=[
                        LLMToolCall(
                            id="blocking-call",
                            name="blocking_tool",
                            arguments={"value": "once"},
                        )
                    ],
                )
            )

    async def scenario() -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition("blocking_tool", "blocking", BlockingArgs, blocking_tool)
        )
        store = InMemorySessionStore()
        llm = ToolLLM()
        controller = AgentController(
            app_settings=settings(tmp_path),
            session_store=store,
            registry=registry,
            llm_client=llm,
        )
        response = await chat_stream(
            request_for(controller),
            ChatRequest(session_id="blocking-cancel", message="run tool"),
        )
        consume = asyncio.create_task(anext(response.body_iterator))
        assert await asyncio.to_thread(started.wait, 3)
        consume.cancel()
        await asyncio.sleep(0)
        with pytest.raises(SessionBusyError):
            controller.acquire_session_run("blocking-cancel")
        release.set()
        with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
            await consume
        await response.body_iterator.aclose()

        replacement = controller.acquire_session_run("blocking-cancel")
        assert store.release_run("blocking-cancel", replacement) is True
        session = store.get("blocking-cancel")
        assert session is not None
        assert len(session.tool_calls) == 1
        assert executed == ["once"]
        assert llm.calls == 1

    asyncio.run(scenario())


def test_guard_close_is_idempotent_and_old_release_is_fenced(tmp_path: Path) -> None:
    del tmp_path

    async def scenario() -> None:
        class CountingStore(InMemorySessionStore):
            def __init__(self) -> None:
                super().__init__()
                self.release_calls = 0

            def release_run(self, session_id: str, run_id: str) -> bool:
                self.release_calls += 1
                return super().release_run(session_id, run_id)

        store = CountingStore()
        old_run = store.acquire_run("guard-fencing", lease_seconds=30)
        guard = StreamRunLeaseGuard(store, "guard-fencing", old_run)
        first, second = await asyncio.gather(guard.close(), guard.close())
        assert sorted((first, second)) == [True, True]
        assert store.release_calls == 1
        new_run = store.acquire_run("guard-fencing", lease_seconds=30)
        assert await guard.close() is True
        assert store.release_calls == 1
        with pytest.raises(SessionBusyError):
            store.acquire_run("guard-fencing", lease_seconds=30)
        assert store.release_run("guard-fencing", new_run) is True

    asyncio.run(scenario())
