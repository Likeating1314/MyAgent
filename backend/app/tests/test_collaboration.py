from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.collaboration import (
    READ_ONLY_COLLABORATION_TOOLS,
    CollaborationOrchestrator,
    build_collaboration_registry,
)
from app.agent.llm_client import LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.tool_executor import ToolContext
from app.agent.tool_registry import ToolDefinition, ToolRegistry
from app.config import AppSettings
from app.main import create_app
from app.models.schemas import CollaborationCreateRequest, CollaborationRunRequest
from app.services.collaboration_store import CollaborationBusyError, CollaborationStore, utc_now
from pydantic import BaseModel


def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        OPENAI_API_KEY="", WORKSPACE_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "collaboration.sqlite3"),
        SESSION_RUN_LEASE_SECONDS=30,
    )


def room_payload(agent_count: int = 3) -> CollaborationCreateRequest:
    return CollaborationCreateRequest.model_validate({
        "session_id": "s1", "title": "研究室", "rounds": 2,
        "agents": [
            {
                "id": f"a{index}", "name": f"Agent {index}",
                "role": "协调者" if index == 0 else "分析师",
                "prompt": "关注证据", "position": index,
                "is_coordinator": index == 0,
            }
            for index in range(agent_count)
        ],
    })


def parse(raw: list[str]) -> list[tuple[str, dict[str, Any]]]:
    result = []
    for item in raw:
        name = next(line[6:].strip() for line in item.splitlines() if line.startswith("event:"))
        data = next(line[5:].strip() for line in item.splitlines() if line.startswith("data:"))
        result.append((name, json.loads(data)))
    return result


class ContextProbeLLM:
    def __init__(self) -> None:
        self.contexts: list[list[dict[str, Any]]] = []

    async def stream_generate(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        self.contexts.append(kwargs["messages"])
        content = f"结论-{len(self.contexts)}"
        yield LLMStreamEvent(content_delta=content)
        yield LLMStreamEvent(result=LLMResult(content=content, tool_calls=[]))


def collect(orchestrator: CollaborationOrchestrator, room_id: str, message: str = "请讨论"):
    async def scenario():
        payload = CollaborationRunRequest(message=message)
        run = orchestrator.acquire_run(room_id, payload)
        return parse([item async for item in orchestrator.stream_run(room_id, payload, run)])
    return asyncio.run(scenario())


@pytest.mark.parametrize("count", [2, 5])
def test_room_accepts_two_to_five_agents(count: int) -> None:
    assert len(room_payload(count).agents) == count


@pytest.mark.parametrize("count", [1, 6])
def test_room_rejects_agent_count_outside_range(count: int) -> None:
    with pytest.raises(ValidationError):
        room_payload(count)


@pytest.mark.parametrize("coordinators", [0, 2])
def test_room_requires_exactly_one_coordinator(coordinators: int) -> None:
    data = room_payload().model_dump()
    for index, agent in enumerate(data["agents"]):
        agent["is_coordinator"] = index < coordinators
    with pytest.raises(ValidationError):
        CollaborationCreateRequest.model_validate(data)


def test_sqlite_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    CollaborationStore(path)
    CollaborationStore(path)
    assert CollaborationStore(path).list() == []


def test_events_have_strict_sequence_and_agent_ownership(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    events = collect(CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=ContextProbeLLM()), room.id)
    snapshot = store.require(room.id)
    assert [event.sequence for event in snapshot.events] == list(range(1, len(snapshot.events) + 1))
    agent_events = [event for event in snapshot.events if event.event.startswith("agent_")]
    assert agent_events and all(event.agent_id in {"a0", "a1"} for event in agent_events)
    assert all(payload["agent_id"] in {"a0", "a1"} for name, payload in events if name.startswith("agent_"))


def test_later_agent_reads_prior_persisted_full_message(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    llm = ContextProbeLLM()
    collect(CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=llm), room.id)
    assert "结论-1" in json.dumps(llm.contexts[1], ensure_ascii=False)
    assert any(event.event == "agent_message" and event.data["content"] == "结论-1" for event in store.require(room.id).events)


def test_server_registry_is_read_only_and_excludes_side_effects() -> None:
    registry = build_collaboration_registry()
    assert set(registry.names()) == set(READ_ONLY_COLLABORATION_TOOLS)
    assert "write_file" not in registry.names()
    assert "run_command" not in registry.names()


class ProbeArgs(BaseModel):
    value: str = "x"


def test_write_and_command_tools_are_never_executed(tmp_path: Path) -> None:
    executed: list[str] = []
    source = ToolRegistry()
    for name in ("write_file", "run_command", "read_file"):
        source.register(ToolDefinition(name, name, ProbeArgs, lambda _c, _a, n=name: executed.append(n)))
    registry = build_collaboration_registry(source)
    assert registry.names() == ["read_file"]
    assert executed == []


def test_same_room_concurrent_run_is_busy(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    store.acquire_run(room.id, "first", lease_seconds=30)
    with pytest.raises(CollaborationBusyError):
        store.acquire_run(room.id, "second", lease_seconds=30)


def test_cancellation_does_not_start_next_agent_and_terminal_is_unique(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(3))
    llm = ContextProbeLLM()
    cancelled = {"value": False}

    async def scenario():
        payload = CollaborationRunRequest(message="stop")
        run = CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=llm)
        acquired = run.acquire_run(room.id, payload)
        async def is_cancelled():
            value = cancelled["value"]
            cancelled["value"] = True
            return value
        return parse([event async for event in run.stream_run(room.id, payload, acquired, is_cancelled=is_cancelled)])

    events = asyncio.run(scenario())
    terminals = [name for name, _ in events if name in {"done", "error", "cancelled"}]
    assert terminals == ["cancelled"]
    assert len(llm.contexts) <= 1
    assert len([event for event in store.require(room.id).events if event.event in {"done", "error", "cancelled"}]) == 1


class FailingLLM:
    async def stream_generate(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        del kwargs
        raise RuntimeError("Authorization: Bearer secret-key\ntraceback secret")
        yield


def test_errors_are_sanitized(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    events = collect(CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=FailingLLM()), room.id)
    serialized = json.dumps(events, ensure_ascii=False)
    assert "secret-key" not in serialized and "traceback" not in serialized
    assert [name for name, _ in events if name in {"done", "error", "cancelled"}] == ["error"]


def test_followup_message_reads_existing_collaboration_history(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    first = ContextProbeLLM()
    collect(CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=first), room.id, "第一问")
    second = ContextProbeLLM()
    collect(CollaborationOrchestrator(app_settings=settings(tmp_path), store=store, llm_client=second), room.id, "继续追问")
    context = json.dumps(second.contexts[0], ensure_ascii=False)
    assert "第一问" in context and "结论-1" in context and "继续追问" in context


def test_collaboration_api_create_snapshot_and_busy_409(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    app_settings.api_auth_token = "test-token-that-is-at-least-32-bytes"
    app = create_app(app_settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}
    session = client.post("/api/sessions", headers=headers, json={"session_id": "s1"})
    assert session.status_code == 200
    created = client.post(
        "/api/collaborations", headers=headers, json=room_payload(2).model_dump()
    )
    assert created.status_code == 201
    room_id = created.json()["id"]
    assert client.get("/api/collaborations?session_id=s1", headers=headers).json()[0]["id"] == room_id
    assert client.get(f"/api/collaborations/{room_id}", headers=headers).status_code == 200
    app.state.collaboration_store.acquire_run(room_id, "active", lease_seconds=30)
    busy = client.post(
        f"/api/collaborations/{room_id}/runs/stream",
        headers=headers,
        json={"message": "second"},
    )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "collaboration_busy"


@pytest.mark.parametrize("fault", ["deleted", "expired"])
def test_lease_loss_after_acquire_emits_error_and_finalizes_run(
    tmp_path: Path, fault: str
) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    orchestrator = CollaborationOrchestrator(
        app_settings=settings(tmp_path), store=store, llm_client=ContextProbeLLM()
    )
    payload = CollaborationRunRequest(message="lease fault")
    run = orchestrator.acquire_run(room.id, payload)
    with store._connect() as connection:
        if fault == "deleted":
            connection.execute(
                "DELETE FROM collaboration_leases WHERE collaboration_id = ?",
                (room.id,),
            )
        else:
            connection.execute(
                "UPDATE collaboration_leases SET expires_at = ? WHERE collaboration_id = ?",
                ((utc_now() - timedelta(seconds=1)).isoformat(), room.id),
            )

    async def scenario():
        return parse([
            event
            async for event in orchestrator.stream_run(room.id, payload, run)
        ])

    events = asyncio.run(scenario())
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["code"] == "collaboration_lease_lost"
    saved_run = next(item for item in store.require(room.id).runs if item.id == run.id)
    assert saved_run.status == "error"
    assert saved_run.terminal_event == "error"
    assert len([
        event for event in store.require(room.id).events
        if event.run_id == run.id and event.event in {"done", "error", "cancelled"}
    ]) == 1


def test_lease_loss_finalization_never_changes_takeover_run(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    orchestrator = CollaborationOrchestrator(
        app_settings=settings(tmp_path), store=store, llm_client=ContextProbeLLM()
    )
    payload = CollaborationRunRequest(message="old")
    old_run = orchestrator.acquire_run(room.id, payload)
    with store._connect() as connection:
        connection.execute(
            "UPDATE collaboration_leases SET expires_at = ? WHERE collaboration_id = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), room.id),
        )
    new_run = store.acquire_run(room.id, "takeover", lease_seconds=30)

    async def scenario():
        return parse([
            event
            async for event in orchestrator.stream_run(room.id, payload, old_run)
        ])

    assert [name for name, _ in asyncio.run(scenario())] == ["error"]
    snapshot = store.require(room.id)
    old_saved = next(item for item in snapshot.runs if item.id == old_run.id)
    new_saved = next(item for item in snapshot.runs if item.id == new_run.id)
    assert (old_saved.status, old_saved.terminal_event) == ("error", "error")
    assert (new_saved.status, new_saved.terminal_event) == ("running", None)
    assert store.renew_run(
        room.id, new_run.id, new_run.fencing_token, lease_seconds=30
    ) is True


@pytest.mark.parametrize("fault", ["deleted", "expired"])
def test_cancellation_with_simultaneous_lease_loss_emits_error_and_finalizes_run(
    tmp_path: Path, fault: str
) -> None:
    store = CollaborationStore(tmp_path / "db.sqlite3")
    room = store.create(room_payload(2))
    orchestrator = CollaborationOrchestrator(
        app_settings=settings(tmp_path), store=store, llm_client=ContextProbeLLM()
    )
    payload = CollaborationRunRequest(message="cancel with lease fault")
    run = orchestrator.acquire_run(room.id, payload)
    injected = False

    async def cancel_and_break_lease() -> bool:
        nonlocal injected
        if injected:
            return True
        injected = True
        with store._connect() as connection:
            if fault == "deleted":
                connection.execute(
                    "DELETE FROM collaboration_leases WHERE collaboration_id = ?",
                    (room.id,),
                )
            else:
                connection.execute(
                    "UPDATE collaboration_leases SET expires_at = ? WHERE collaboration_id = ?",
                    ((utc_now() - timedelta(seconds=1)).isoformat(), room.id),
                )
        return True

    async def scenario():
        return parse([
            event
            async for event in orchestrator.stream_run(
                room.id, payload, run, is_cancelled=cancel_and_break_lease
            )
        ])

    events = asyncio.run(scenario())
    assert [name for name, _ in events] == ["run_started", "error"]
    assert events[-1][1]["code"] == "collaboration_lease_lost"
    snapshot = store.require(room.id)
    saved_run = next(item for item in snapshot.runs if item.id == run.id)
    assert (saved_run.status, saved_run.terminal_event) == ("error", "error")
    assert len([
        event for event in snapshot.events
        if event.run_id == run.id and event.event in {"done", "error", "cancelled"}
    ]) == 1
