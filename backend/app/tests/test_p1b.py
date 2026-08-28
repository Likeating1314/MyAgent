from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.agent.context_builder import ContextBuilder, OMISSION_MARKER
from app.agent.controller import AgentController
from app.agent.llm_client import LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.memory import utc_now
from app.agent.tool_executor import ToolContext, ToolExecutor
from app.agent.tool_registry import ToolDefinition, ToolRegistry, build_default_registry
from app.config import AppSettings
from app.main import create_app
from app.models.schemas import ChatRequest, ToolCallRecord
from app.services.approval_store import ApprovalStore
from app.services.session_store import (
    InMemorySessionStore,
    SessionLeaseLostError,
    SQLiteSessionStore,
)
from app.tools.command_tools import RunCommandArgs


class LeaseProbeArgs(BaseModel):
    value: str


def settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    values = {
        "WORKSPACE_DIR": str(tmp_path / "workspace"),
        "SQLITE_PATH": str(tmp_path / "agent.sqlite3"),
        "OPENAI_API_KEY": "",
        "ALLOW_COMMAND_EXECUTION": False,
        "API_AUTH_TOKEN": "test-token-that-is-at-least-32-bytes",
        "SESSION_RUN_LEASE_SECONDS": 5,
    }
    values.update(overrides)
    return AppSettings(**values)


def parse_events(raw: list[str]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for item in raw:
        lines = item.splitlines()
        name = next(line[6:].strip() for line in lines if line.startswith("event:"))
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        events.append((name, json.loads(data)))
    return events


def test_legacy_session_migration_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    messages = [{"role": "user", "content": "legacy"}, {"role": "assistant", "content": "answer"}]
    tools = [{"name": "read_file", "arguments": {"path": "a.txt"}, "status": "ok", "result": {}}]
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                messages_json TEXT NOT NULL, tool_calls_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("legacy", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
             json.dumps(messages), json.dumps(tools)),
        )

    first = SQLiteSessionStore(database).get("legacy")
    second = SQLiteSessionStore(database).get("legacy")

    assert first is not None and second is not None
    assert first.messages == second.messages == messages
    assert len(first.tool_calls) == len(second.tool_calls) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id = 'legacy'"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM session_event_migrations WHERE session_id = 'legacy'"
        ).fetchone()[0] == 1


def test_corrupt_legacy_json_fails_migration_without_marking_complete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "corrupt.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                messages_json TEXT NOT NULL, tool_calls_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("broken", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "{", "[]"),
        )

    with pytest.raises(RuntimeError, match="旧会话数据损坏"):
        SQLiteSessionStore(database)
    assert "session_id=broken" in caplog.text


def test_concurrent_appends_have_unique_gapless_sequences(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    store = SQLiteSessionStore(database)

    def append(worker: int) -> None:
        for item in range(10):
            store.append_event("shared", "message", {"role": "user", "content": f"{worker}-{item}"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(8)))

    session = store.get("shared")
    assert session is not None
    assert len(session.messages) == 80
    assert len({message["content"] for message in session.messages}) == 80
    with sqlite3.connect(database) as connection:
        sequences = [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM session_events WHERE session_id = 'shared' ORDER BY sequence"
            )
        ]
    assert sequences == list(range(1, 81))


def test_tool_triplet_append_is_atomic(tmp_path: Path) -> None:
    database = tmp_path / "atomic.sqlite3"
    store = SQLiteSessionStore(database)
    store.get_or_create("atomic")
    assistant = {
        "role": "assistant",
        "content": "tool",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
    }
    record = ToolCallRecord(name="probe", status="ok", result={"ok": True})
    tool_message = {"role": "tool", "tool_call_id": "call-1", "content": "{}"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_tool_event BEFORE INSERT ON session_events
            WHEN NEW.event_type = 'tool_call' BEGIN SELECT RAISE(ABORT, 'reject'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            "atomic",
            [("message", assistant), ("tool_call", record), ("message", tool_message)],
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id = 'atomic'"
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_tool_event")
    sequences = store.append_batch(
        "atomic", [("message", assistant), ("tool_call", record), ("message", tool_message)]
    )
    assert sequences == [1, 2, 3]


def test_api_returns_409_for_busy_sync_and_stream_sessions(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    run_id = app.state.session_store.acquire_run("busy", lease_seconds=30)
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}
    with TestClient(app) as client:
        sync_response = client.post("/api/chat", headers=headers, json={"session_id": "busy", "message": "run"})
        stream_response = client.post(
            "/api/chat/stream", headers=headers, json={"session_id": "busy", "message": "run"}
        )
    app.state.session_store.release_run("busy", run_id)
    assert sync_response.status_code == 409
    assert stream_response.status_code == 409
    assert sync_response.json()["detail"]["code"] == "session_busy"
    assert stream_response.json()["detail"]["code"] == "session_busy"


def test_api_rejects_oversized_user_message_before_starting_stream(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, MAX_USER_MESSAGE_CHARS=4))
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}
    payload = {"session_id": "large", "message": "12345"}
    with TestClient(app) as client:
        sync_response = client.post("/api/chat", headers=headers, json=payload)
        stream_response = client.post("/api/chat/stream", headers=headers, json=payload)
    assert sync_response.status_code == 413
    assert stream_response.status_code == 413
    assert sync_response.json()["detail"]["code"] == "message_too_large"
    assert stream_response.json()["detail"]["code"] == "message_too_large"


def test_different_sessions_execute_concurrently(tmp_path: Path) -> None:
    entered = 0
    maximum = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    class BlockingLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            nonlocal entered, maximum
            with lock:
                entered += 1
                maximum = max(maximum, entered)
                if entered == 2:
                    both_started.set()
            release.wait(timeout=3)
            with lock:
                entered -= 1
            return LLMResult(content="done", tool_calls=[])

    controller = AgentController(app_settings=settings(tmp_path), llm_client=BlockingLLM())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(controller.handle_chat, ChatRequest(session_id=session_id, message="run"))
            for session_id in ("one", "two")
        ]
        assert both_started.wait(timeout=2)
        release.set()
        responses = [future.result(timeout=3) for future in futures]
    assert maximum == 2
    assert [response.answer for response in responses] == ["done", "done"]


def test_done_error_and_cancelled_release_session_run(tmp_path: Path) -> None:
    store = InMemorySessionStore()

    class DoneLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            return LLMResult(content="done", tool_calls=[])

    done_controller = AgentController(
        app_settings=settings(tmp_path), session_store=store, llm_client=DoneLLM()
    )
    done_controller.handle_chat(ChatRequest(session_id="done", message="run"))
    token = store.acquire_run("done", lease_seconds=5)
    store.release_run("done", token)

    class ErrorLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            raise RuntimeError("failure")

    error_controller = AgentController(
        app_settings=settings(tmp_path), session_store=store, llm_client=ErrorLLM()
    )
    with pytest.raises(RuntimeError):
        error_controller.handle_chat(ChatRequest(session_id="error", message="run"))
    token = store.acquire_run("error", lease_seconds=5)
    store.release_run("error", token)

    async def cancelled() -> list[str]:
        async def is_cancelled() -> bool:
            return True

        controller = AgentController(app_settings=settings(tmp_path), session_store=store)
        return [
            item
            async for item in controller.handle_chat_stream(
                ChatRequest(session_id="cancelled", message="run"), is_cancelled=is_cancelled
            )
        ]

    assert parse_events(asyncio.run(cancelled()))[-1][0] == "cancelled"
    token = store.acquire_run("cancelled", lease_seconds=5)
    store.release_run("cancelled", token)


def test_expired_session_run_can_be_recovered(tmp_path: Path) -> None:
    database = tmp_path / "lease.sqlite3"
    store = SQLiteSessionStore(database)
    original = store.acquire_run("lease", lease_seconds=30)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session_runs SET expires_at = ? WHERE session_id = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), "lease"),
        )
    replacement = store.acquire_run("lease", lease_seconds=30)
    assert replacement != original
    with pytest.raises(SessionLeaseLostError):
        store.append_event(
            "lease", "message", {"role": "user", "content": "stale"}, run_id=original
        )
    assert store.release_run("lease", original) is False
    assert store.release_run("lease", replacement) is True


def test_context_budget_keeps_current_user_and_complete_tool_groups() -> None:
    messages = [
        {"role": "user", "content": "old" * 500},
        {
            "role": "assistant",
            "content": "old tool",
            "tool_calls": [{"id": "old-call", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "old result" * 500},
        {"role": "user", "content": "CURRENT"},
        {
            "role": "assistant",
            "content": "new tool",
            "tool_calls": [{"id": "new-call", "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "new-call", "content": "recent evidence"},
    ]
    builder = ContextBuilder(max_chars=1_200)

    context = builder.build(messages, current_user_index=3)

    assert builder.serialized_chars(context) <= 1_200
    assert any(message.get("content") == "CURRENT" for message in context)
    assert any(message.get("content") == OMISSION_MARKER for message in context)
    serialized = json.dumps(context, ensure_ascii=False)
    assert ("new-call" in serialized) == ("recent evidence" in serialized)
    assert "old-call" not in serialized
    assert not any(message.get("role") == "tool" and "tool_call_id" not in message for message in context)


def test_large_command_output_is_drained_and_persistable_as_bounded_json(tmp_path: Path) -> None:
    database = tmp_path / "command.sqlite3"
    approval_store = ApprovalStore(database)
    context = ToolContext(
        workspace_dir=tmp_path,
        session_id="command",
        allow_command_execution=True,
        approval_store=approval_store,
        max_result_chars=1_200,
        max_command_output_chars=4_000,
    )
    executor = ToolExecutor(build_default_registry(), context)
    command = [
        sys.executable,
        "-c",
        "import sys;sys.stdout.write('o'*200000);sys.stderr.write('e'*200000)",
    ]
    args = RunCommandArgs(command=command, timeout_seconds=10)
    pending = executor.execute("run_command", args.model_dump())
    assert pending.status == "error"
    approval = approval_store.list_requests(status="pending")[0]
    approval_store.set_status(approval.id, "approved")

    record = executor.execute(
        "run_command", args.model_copy(update={"approval_id": approval.id}).model_dump()
    )

    assert record.status == "ok"
    assert record.result["truncated"] is True
    assert record.result["original_chars"] > context.max_result_chars
    assert len(json.dumps(record.result, ensure_ascii=False, separators=(",", ":"))) <= context.max_result_chars
    store = SQLiteSessionStore(database)
    store.append_event("command", "tool_call", record)
    assert store.get("command").tool_calls[0].result == record.result


def test_tool_batch_save_failure_reports_session_error(tmp_path: Path) -> None:
    class FailingStore(InMemorySessionStore):
        def append_batch(self, session_id, events, **kwargs):  # noqa: ANN001
            if any(event_type == "tool_call" for event_type, _ in events):
                raise sqlite3.OperationalError("database unavailable")
            return super().append_batch(session_id, events, **kwargs)

    class ToolLLM:
        async def stream_generate(self, **kwargs: Any):
            del kwargs
            yield LLMStreamEvent(
                result=LLMResult(
                    content=None,
                    tool_calls=[
                        LLMToolCall(
                            id="call-1", name="list_files", arguments={"path": ".", "max_depth": 0}
                        )
                    ],
                )
            )

    store = FailingStore()
    controller = AgentController(
        app_settings=settings(tmp_path), session_store=store, llm_client=ToolLLM()
    )

    async def collect() -> list[str]:
        return [
            item
            async for item in controller.handle_chat_stream(
                ChatRequest(session_id="save-error", message="run")
            )
        ]

    events = parse_events(asyncio.run(collect()))
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["code"] == "session_error"


def test_sync_heartbeat_lease_loss_prevents_tool_execution(tmp_path: Path) -> None:
    class LosingStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.renewed = threading.Event()

        def renew_run(self, session_id, run_id, *, lease_seconds):  # noqa: ANN001
            del session_id, run_id, lease_seconds
            self.renewed.set()
            return False

    store = LosingStore()
    executed: list[str] = []

    def probe(context: ToolContext, args: LeaseProbeArgs) -> dict[str, str]:
        del context
        executed.append(args.value)
        return {"value": args.value}

    class BlockedLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            assert store.renewed.wait(timeout=3)
            return LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="call-1", name="probe", arguments={"value": "no"})],
            )

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", LeaseProbeArgs, probe))
    controller = AgentController(
        app_settings=settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=BlockedLLM(),
    )

    with pytest.raises(SessionLeaseLostError):
        controller.handle_chat(ChatRequest(session_id="sync-lease-lost", message="run"))

    assert executed == []


def test_async_heartbeat_lease_loss_emits_one_terminal_and_skips_tool(tmp_path: Path) -> None:
    class LosingStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.renewed = threading.Event()

        def renew_run(self, session_id, run_id, *, lease_seconds):  # noqa: ANN001
            del session_id, run_id, lease_seconds
            self.renewed.set()
            return False

    store = LosingStore()
    executed: list[str] = []

    def probe(context: ToolContext, args: LeaseProbeArgs) -> dict[str, str]:
        del context
        executed.append(args.value)
        return {"value": args.value}

    class BlockedLLM:
        async def stream_generate(self, **kwargs: Any):
            del kwargs
            await asyncio.to_thread(store.renewed.wait, 3)
            yield LLMStreamEvent(
                result=LLMResult(
                    content=None,
                    tool_calls=[
                        LLMToolCall(id="call-1", name="probe", arguments={"value": "no"})
                    ],
                )
            )

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", LeaseProbeArgs, probe))
    controller = AgentController(
        app_settings=settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=BlockedLLM(),
    )

    async def collect() -> list[str]:
        return [
            item
            async for item in controller.handle_chat_stream(
                ChatRequest(session_id="async-lease-lost", message="run")
            )
        ]

    events = parse_events(asyncio.run(collect()))
    terminals = [event for event in events if event[0] in {"done", "error", "cancelled"}]
    assert terminals == [
        (
            "error",
            {
                "code": "session_lease_lost",
                "message": "会话执行权已失效，请重试。",
                "session_id": "async-lease-lost",
            },
        )
    ]
    assert executed == []


@pytest.mark.parametrize("streaming", [False, True])
def test_lease_renewal_exception_fails_closed_before_tool(
    tmp_path: Path, streaming: bool
) -> None:
    class FailingRenewStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.renewed = threading.Event()

        def renew_run(self, session_id, run_id, *, lease_seconds):  # noqa: ANN001
            del session_id, run_id, lease_seconds
            self.renewed.set()
            raise sqlite3.OperationalError("renew unavailable")

    store = FailingRenewStore()
    executed: list[str] = []

    def probe(context: ToolContext, args: LeaseProbeArgs) -> dict[str, str]:
        del context
        executed.append(args.value)
        return {"value": args.value}

    tool_result = LLMResult(
        content=None,
        tool_calls=[LLMToolCall(id="call-1", name="probe", arguments={"value": "no"})],
    )

    class BlockingLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            assert store.renewed.wait(timeout=3)
            return tool_result

        async def stream_generate(self, **kwargs: Any):
            del kwargs
            await asyncio.to_thread(store.renewed.wait, 3)
            yield LLMStreamEvent(result=tool_result)

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", LeaseProbeArgs, probe))
    controller = AgentController(
        app_settings=settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=BlockingLLM(),
    )

    if streaming:
        async def collect() -> list[str]:
            return [
                item
                async for item in controller.handle_chat_stream(
                    ChatRequest(session_id="renew-error-stream", message="run")
                )
            ]

        events = parse_events(asyncio.run(collect()))
        assert [(name, payload["code"]) for name, payload in events] == [
            ("error", "session_lease_lost")
        ]
    else:
        with pytest.raises(SessionLeaseLostError):
            controller.handle_chat(ChatRequest(session_id="renew-error-sync", message="run"))

    assert executed == []


def test_replacement_run_blocks_old_run_tool_and_survives_old_release(tmp_path: Path) -> None:
    database = tmp_path / "takeover.sqlite3"
    store = SQLiteSessionStore(database)
    model_started = threading.Event()
    release_model = threading.Event()
    executed: list[str] = []

    def probe(context: ToolContext, args: LeaseProbeArgs) -> dict[str, str]:
        del context
        executed.append(args.value)
        return {"value": args.value}

    class BlockedLLM:
        def generate(self, **kwargs: Any) -> LLMResult:
            del kwargs
            model_started.set()
            assert release_model.wait(timeout=3)
            return LLMResult(
                content=None,
                tool_calls=[LLMToolCall(id="call-1", name="probe", arguments={"value": "no"})],
            )

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", LeaseProbeArgs, probe))
    controller = AgentController(
        app_settings=settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=BlockedLLM(),
    )
    old_run = store.acquire_run("takeover", lease_seconds=30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            controller.handle_chat,
            ChatRequest(session_id="takeover", message="run"),
            run_id=old_run,
        )
        assert model_started.wait(timeout=2)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE session_runs SET expires_at = ? WHERE session_id = ?",
                ((utc_now() - timedelta(seconds=1)).isoformat(), "takeover"),
            )
        replacement = store.acquire_run("takeover", lease_seconds=30)
        release_model.set()
        with pytest.raises(SessionLeaseLostError):
            future.result(timeout=3)

    assert executed == []
    assert store.release_run("takeover", old_run) is False
    assert store.release_run("takeover", replacement) is True


def test_tool_side_effect_cannot_be_undone_when_append_fencing_rejects_old_run(
    tmp_path: Path,
) -> None:
    class TakeoverOnToolAppendStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.replacement_run: str | None = None

        def append_batch(self, session_id, events, **kwargs):  # noqa: ANN001
            if any(event_type == "tool_call" for event_type, _ in events):
                with self._lock:
                    self.replacement_run = "replacement-run"
                    self._runs[session_id] = (
                        self.replacement_run,
                        utc_now() + timedelta(seconds=30),
                    )
            return super().append_batch(session_id, events, **kwargs)

    store = TakeoverOnToolAppendStore()
    side_effects: list[str] = []

    def probe(context: ToolContext, args: LeaseProbeArgs) -> dict[str, str]:
        del context
        side_effects.append(args.value)
        return {"value": args.value}

    class ToolLLM:
        async def stream_generate(self, **kwargs: Any):
            del kwargs
            yield LLMStreamEvent(
                result=LLMResult(
                    content=None,
                    tool_calls=[
                        LLMToolCall(
                            id="call-1", name="probe", arguments={"value": "already-started"}
                        )
                    ],
                )
            )

    registry = ToolRegistry()
    registry.register(ToolDefinition("probe", "probe", LeaseProbeArgs, probe))
    controller = AgentController(
        app_settings=settings(tmp_path),
        session_store=store,
        registry=registry,
        llm_client=ToolLLM(),
    )

    async def collect() -> list[str]:
        return [
            item
            async for item in controller.handle_chat_stream(
                ChatRequest(session_id="append-takeover", message="run")
            )
        ]

    events = parse_events(asyncio.run(collect()))
    # Fencing protects session persistence; it cannot undo a tool that already ran.
    assert side_effects == ["already-started"]
    assert [(name, payload["code"]) for name, payload in events] == [
        ("error", "session_lease_lost")
    ]
    session = store.get("append-takeover")
    assert session is not None
    assert session.messages == [{"role": "user", "content": "run"}]
    assert session.tool_calls == []
    assert store.replacement_run is not None
    assert store.release_run("append-takeover", store.replacement_run) is True


def test_sync_api_maps_lease_loss_to_409(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))

    def lose_lease(payload):  # noqa: ANN001
        del payload
        raise SessionLeaseLostError("internal lease detail")

    app.state.controller.handle_chat = lose_lease
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            headers=headers,
            json={"session_id": "lost", "message": "run"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "session_lease_lost",
        "message": "会话执行权已失效，请重试。",
    }
