from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.controller import AgentController
from app.agent.llm_client import LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.memory import utc_now
from app.config import AppSettings
from app.main import create_app
from app.models.schemas import AgentSettings, ApprovalResumeRequest, ChatRequest
from app.services.approval_store import ApprovalResumeError, ApprovalStore
from app.services.session_store import (
    InMemorySessionStore,
    SessionArchivedError,
    SessionBusyError,
    SessionOpenApprovalError,
    SQLiteSessionStore,
)
import app.tools.file_tools as file_tools


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


class FinalAsyncLLM:
    def __init__(self, content: str = "续跑完成") -> None:
        self.content = content
        self.calls = 0

    async def stream_generate(self, **kwargs: Any):  # noqa: ANN201
        del kwargs
        self.calls += 1
        yield LLMStreamEvent(content_delta=self.content)
        yield LLMStreamEvent(result=LLMResult(content=self.content, tool_calls=[]))


def parse_events(raw: list[str]) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for event in raw:
        name = next(line[6:].strip() for line in event.splitlines() if line.startswith("event:"))
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        parsed.append((name, json.loads(data)))
    return parsed


def approved_write(store: ApprovalStore, session_id: str, *, content: str = "once"):
    approval = store.create_pending(
        session_id=session_id,
        tool_name="write_file",
        arguments={
            "path": "resumed.txt",
            "content": content,
            "overwrite": True,
            "expected_sha256": None,
        },
        reason="create file",
    )
    return store.set_status(approval.id, "approved")


def collect_resume(
    controller: AgentController,
    approval_id: str,
    session_id: str,
    *,
    request_settings: AgentSettings | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    async def collect() -> list[str]:
        run_id = controller.acquire_session_run(session_id)
        return [
            event
            async for event in controller.handle_approval_resume_stream(
                approval_id,
                ApprovalResumeRequest(settings=request_settings),
                run_id=run_id,
            )
        ]

    return parse_events(asyncio.run(collect()))


def test_approved_tool_executes_once_and_continues_without_duplicate_user(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("resume")
    session_store.append_event("resume", "message", {"role": "user", "content": "write it"})
    approval = approved_write(approval_store, "resume")
    llm = FinalAsyncLLM()
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=llm,
    )

    events = collect_resume(controller, approval.id, "resume")

    assert [name for name, _ in events] == ["tool_call", "delta", "done"]
    assert (app_settings.resolved_workspace_dir() / "resumed.txt").read_text() == "once"
    assert approval_store.get(approval.id).status == "consumed"
    session = session_store.get("resume")
    assert session is not None
    assert [message["role"] for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert sum(message["role"] == "user" for message in session.messages) == 1
    tool_call_id = session.messages[1]["tool_calls"][0]["id"]
    assert tool_call_id.startswith("resume_")
    assert session.messages[2]["tool_call_id"] == tool_call_id
    assert llm.calls == 1

    with pytest.raises(ApprovalResumeError) as repeated:
        approval_store.require_resumable(approval.id)
    assert repeated.value.code == "approval_consumed"
    assert (app_settings.resolved_workspace_dir() / "resumed.txt").read_text() == "once"


@pytest.mark.parametrize(
    ("state", "code"),
    [("pending", "approval_pending"), ("rejected", "approval_rejected")],
)
def test_non_approved_states_never_resume(tmp_path: Path, state: str, code: str) -> None:
    store = ApprovalStore(tmp_path / "approval.sqlite3")
    approval = store.create_pending(
        session_id="s", tool_name="write_file", arguments={"path": "x"}, reason="test"
    )
    if state == "rejected":
        store.set_status(approval.id, "rejected")
    with pytest.raises(ApprovalResumeError) as caught:
        store.require_resumable(approval.id)
    assert caught.value.code == code
    assert not (tmp_path / "x").exists()


def test_expired_approval_never_resumes(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    store = ApprovalStore(database)
    approval = approved_write(store, "s")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approvals SET expires_at = ? WHERE id = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), approval.id),
        )
    with pytest.raises(ApprovalResumeError) as caught:
        store.require_resumable(approval.id)
    assert caught.value.code == "approval_expired"


def test_resume_request_rejects_client_session_and_tool_override(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    app.state.session_store.get_or_create("real-session")
    app.state.session_store.append_event(
        "real-session", "message", {"role": "user", "content": "original"}
    )
    approval = approved_write(app.state.approval_store, "real-session")
    client = TestClient(app)
    response = client.post(
        f"/api/approvals/{approval.id}/resume/stream",
        headers={"Authorization": "Bearer test-token-that-is-at-least-32-bytes"},
        json={"session_id": "other", "tool_name": "read_file", "arguments": {}, "settings": {}},
    )
    assert response.status_code == 422
    assert app.state.approval_store.get(approval.id).status == "approved"
    assert not (settings(tmp_path).resolved_workspace_dir() / "resumed.txt").exists()


def test_model_cannot_inject_approval_id_or_see_it_in_tool_schema(tmp_path: Path) -> None:
    class InjectingLLM:
        def __init__(self, approval_id: str) -> None:
            self.approval_id = approval_id
            self.calls = 0

        async def stream_generate(self, **kwargs: Any):  # noqa: ANN201
            self.calls += 1
            if self.calls == 1:
                yield LLMStreamEvent(
                    result=LLMResult(
                        content=None,
                        tool_calls=[
                            LLMToolCall(
                                id="model-call",
                                name="write_file",
                                arguments={
                                    "path": "resumed.txt",
                                    "content": "bypass",
                                    "approval_id": self.approval_id,
                                },
                            )
                        ],
                    )
                )
            else:
                yield LLMStreamEvent(result=LLMResult(content="blocked", tool_calls=[]))

    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    approval = approved_write(approval_store, "model-bypass", content="bypass")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=InjectingLLM(approval.id),
    )

    async def collect() -> list[str]:
        return [
            event
            async for event in controller.handle_chat_stream(
                ChatRequest(session_id="model-bypass", message="do not bypass")
            )
        ]

    events = parse_events(asyncio.run(collect()))
    write_schema = next(
        schema["function"]["parameters"]
        for schema in controller.registry.list_tool_schemas()
        if schema["function"]["name"] == "write_file"
    )
    assert "approval_id" not in write_schema["properties"]
    assert events[-1][0] == "done"
    assert approval_store.get(approval.id).status == "approved"
    assert not (app_settings.resolved_workspace_dir() / "resumed.txt").exists()
    session = session_store.get("model-bypass")
    assert session is not None
    assert session.tool_calls[0].status == "error"
    assert "approval_id" not in session.tool_calls[0].arguments


def test_resume_lease_loss_before_tool_has_no_side_effect(tmp_path: Path) -> None:
    class LostStore(InMemorySessionStore):
        def renew_run(self, session_id, run_id, *, lease_seconds):  # noqa: ANN001
            del session_id, run_id, lease_seconds
            return False

    app_settings = settings(tmp_path)
    approval_store = ApprovalStore(tmp_path / "approval.sqlite3")
    session_store = LostStore()
    session_store.get_or_create("lost")
    session_store.append_event("lost", "message", {"role": "user", "content": "write"})
    approval = approved_write(approval_store, "lost")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )

    events = collect_resume(controller, approval.id, "lost")

    assert events == [
        (
            "error",
            {
                "code": "session_lease_lost",
                "message": "会话执行权已失效，请重试。",
                "session_id": "lost",
            },
        )
    ]
    assert approval_store.get(approval.id).status == "approved"
    assert not (app_settings.resolved_workspace_dir() / "resumed.txt").exists()


def test_concurrent_double_click_executes_side_effect_once(monkeypatch, tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("double")
    session_store.append_event(
        "double", "message", {"role": "user", "content": "write once"}
    )
    approval = approved_write(approval_store, "double")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )
    started = threading.Event()
    release = threading.Event()
    replacements = 0
    real_replace = file_tools.os.replace

    def blocking_replace(source, target):  # noqa: ANN001
        nonlocal replacements
        replacements += 1
        started.set()
        assert release.wait(timeout=3)
        return real_replace(source, target)

    monkeypatch.setattr(file_tools.os, "replace", blocking_replace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(collect_resume, controller, approval.id, "double")
        assert started.wait(timeout=3)
        with pytest.raises(SessionBusyError):
            controller.acquire_session_run("double")
        release.set()
        first_events = first.result(timeout=5)

    assert first_events[-1][0] == "done"
    assert replacements == 1
    with pytest.raises(ApprovalResumeError) as repeated:
        approval_store.require_resumable(approval.id)
    assert repeated.value.code == "approval_consumed"
    assert replacements == 1


def test_resume_fencing_rejects_tool_record_after_takeover(tmp_path: Path) -> None:
    class TakeoverStore(InMemorySessionStore):
        replacement_run = ""

        def append_batch(self, session_id, events, **kwargs):  # noqa: ANN001
            if len(events) == 3 and not self.replacement_run:
                with self._lock:
                    self.replacement_run = "replacement"
                    self._runs[session_id] = (
                        self.replacement_run,
                        utc_now() + timedelta(seconds=30),
                    )
            return super().append_batch(session_id, events, **kwargs)

    app_settings = settings(tmp_path)
    approval_store = ApprovalStore(tmp_path / "approval.sqlite3")
    session_store = TakeoverStore()
    session_store.get_or_create("takeover")
    session_store.append_event(
        "takeover", "message", {"role": "user", "content": "write"}
    )
    approval = approved_write(approval_store, "takeover")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )

    events = collect_resume(controller, approval.id, "takeover")

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "session_lease_lost"
    assert (app_settings.resolved_workspace_dir() / "resumed.txt").read_text() == "once"
    assert approval_store.get(approval.id).status == "consumed"
    session = session_store.get("takeover")
    assert session is not None
    assert session.messages == [{"role": "user", "content": "write"}]
    assert session.tool_calls == []
    assert session_store.release_run("takeover", session_store.replacement_run) is True


def test_session_metadata_migration_is_idempotent_and_legacy_visible(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
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
            "INSERT INTO sessions VALUES (?, ?, ?, '[]', '[]')",
            ("legacy", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

    first = SQLiteSessionStore(database)
    second = SQLiteSessionStore(database)

    assert first.get("legacy").display_title == "legacy"
    assert [session.session_id for session in second.list_sessions()] == ["legacy"]
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        assert {"display_title", "archived_at"} <= columns
        assert connection.execute(
            "SELECT COUNT(*) FROM session_event_migrations WHERE session_id = 'legacy'"
        ).fetchone()[0] == 1


def test_session_lifecycle_constraints_and_archived_read_only(tmp_path: Path) -> None:
    database = tmp_path / "agent.sqlite3"
    store = SQLiteSessionStore(database)
    approvals = ApprovalStore(database)
    store.get_or_create("session")
    run_id = store.acquire_run("session", lease_seconds=30)
    with pytest.raises(SessionBusyError):
        store.rename_session("session", "Busy")
    with pytest.raises(SessionBusyError):
        store.set_archived("session", archived=True)
    store.release_run("session", run_id)

    renamed = store.rename_session("session", "Renamed")
    assert renamed.display_title == "Renamed"
    pending = approvals.create_pending(
        session_id="session", tool_name="write_file", arguments={"path": "x"}, reason="test"
    )
    with pytest.raises(SessionOpenApprovalError):
        store.set_archived("session", archived=True)
    approvals.set_status(pending.id, "rejected")

    archived = store.set_archived("session", archived=True)
    assert archived.archived_at is not None
    assert store.list_sessions() == []
    assert [item.session_id for item in store.list_sessions(archived=True)] == ["session"]
    with pytest.raises(SessionArchivedError):
        store.acquire_run("session", lease_seconds=30)
    with pytest.raises(SessionArchivedError):
        store.append_event("session", "message", {"role": "user", "content": "blocked"})
    with pytest.raises(SessionArchivedError):
        store.rename_session("session", "Blocked")
    assert store.set_archived("session", archived=False).archived_at is None


def test_session_lifecycle_api_errors_and_archive_listing(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}
    assert client.post("/api/sessions", headers=headers, json={"session_id": "life"}).status_code == 200

    invalid = client.patch(
        "/api/sessions/life", headers=headers, json={"display_title": "   "}
    )
    assert invalid.status_code == 422
    control = client.patch(
        "/api/sessions/life", headers=headers, json={"display_title": "bad\u0001title"}
    )
    assert control.status_code == 422
    renamed = client.patch(
        "/api/sessions/life", headers=headers, json={"display_title": "  Useful title  "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_title"] == "Useful title"

    run_id = app.state.session_store.acquire_run("life", lease_seconds=30)
    busy = client.patch(
        "/api/sessions/life", headers=headers, json={"display_title": "Blocked"}
    )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "session_busy"
    app.state.session_store.release_run("life", run_id)

    pending = app.state.approval_store.create_pending(
        session_id="life", tool_name="write_file", arguments={"path": "x"}, reason="test"
    )
    blocked = client.post("/api/sessions/life/archive", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "session_open_approval"
    app.state.approval_store.set_status(pending.id, "rejected")

    archived = client.post("/api/sessions/life/archive", headers=headers)
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get("/api/sessions", headers=headers).json() == []
    archived_list = client.get("/api/sessions?archived=true", headers=headers).json()
    assert [item["session_id"] for item in archived_list] == ["life"]
    assert client.get("/api/sessions/life", headers=headers).status_code == 200
    blocked_chat = client.post(
        "/api/chat", headers=headers, json={"session_id": "life", "message": "blocked"}
    )
    assert blocked_chat.status_code == 409
    assert blocked_chat.json()["detail"]["code"] == "session_archived"
    restored = client.post("/api/sessions/life/unarchive", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None

    missing = client.get("/api/sessions/missing", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "session_not_found"


def test_api_key_is_not_persisted_by_resume(tmp_path: Path, caplog) -> None:
    secret = "sk-secret-value-never-persist"
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("secret")
    session_store.append_event("secret", "message", {"role": "user", "content": "write"})
    approval = approved_write(approval_store, "secret", content="safe")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )
    events = collect_resume(
        controller,
        approval.id,
        "secret",
        request_settings=AgentSettings(api_key=secret),
    )
    assert events[-1][0] == "done"
    assert secret.encode() not in database.read_bytes()
    assert secret not in json.dumps(events, ensure_ascii=False)
    assert secret not in caplog.text


def test_concurrent_pending_approval_creation_reuses_one_request(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approval.sqlite3")
    arguments = {
        "path": "same.txt",
        "content": "same",
        "overwrite": True,
        "expected_sha256": None,
    }

    def create() -> str:
        return store.create_pending(
            session_id="dedupe",
            tool_name="write_file",
            arguments=arguments,
            reason="same request",
        ).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        approval_ids = list(pool.map(lambda _: create(), range(16)))

    assert len(set(approval_ids)) == 1
    pending = store.list_requests(status="pending")
    assert [request.id for request in pending] == [approval_ids[0]]


def test_stale_file_resume_preserves_target_and_reuses_new_pending(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    workspace = app_settings.resolved_workspace_dir()
    target = workspace / "stale.txt"
    target.write_text("original", encoding="utf-8")
    original_hash = hashlib.sha256(b"original").hexdigest()
    arguments = {
        "path": "stale.txt",
        "content": "approved replacement",
        "overwrite": True,
        "expected_sha256": original_hash,
    }
    old = approval_store.create_pending(
        session_id="stale",
        tool_name="write_file",
        arguments=arguments,
        reason="overwrite stale.txt",
    )
    approval_store.set_status(old.id, "approved")
    target.write_text("changed after approval", encoding="utf-8")
    session_store.get_or_create("stale")
    session_store.append_event("stale", "message", {"role": "user", "content": "update"})
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )

    first_events = collect_resume(controller, old.id, "stale")
    second_events = collect_resume(controller, old.id, "stale")

    assert first_events[-1][0] == "done"
    assert second_events == [
        (
            "error",
            {
                "code": "approval_replaced",
                "message": "该审批已由新的待审批请求替代，请使用替代审批继续。",
                "session_id": "stale",
            },
        )
    ]
    assert target.read_text(encoding="utf-8") == "changed after approval"
    refreshed_old = approval_store.get(old.id)
    assert refreshed_old.status == "approved"
    pending = approval_store.list_requests(status="pending")
    assert len(pending) == 1
    assert pending[0].id != old.id
    assert refreshed_old.replacement_approval_id == pending[0].id
    assert pending[0].arguments["expected_sha256"] == hashlib.sha256(
        b"changed after approval"
    ).hexdigest()
    session = session_store.get("stale")
    assert session is not None
    matching = [record for record in session.tool_calls if record.arguments.get("approval_id") == old.id]
    assert len(matching) == 1
    assert all(record.status == "error" for record in matching)


def test_task_cancel_before_first_approved_tool_persists_cancelled(tmp_path: Path) -> None:
    class BlockingApprovalStore(ApprovalStore):
        started = threading.Event()
        release = threading.Event()

        def require_resumable(self, request_id):  # noqa: ANN001, ANN201
            self.started.set()
            assert self.release.wait(timeout=3)
            return super().require_resumable(request_id)

    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = BlockingApprovalStore(database)
        session_store.get_or_create("cancel-before-tool")
        session_store.append_event(
            "cancel-before-tool", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "cancel-before-tool")
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=FinalAsyncLLM(),
        )
        run_id = controller.acquire_session_run("cancel-before-tool")

        async def consume() -> list[str]:
            return [
                event
                async for event in controller.handle_approval_resume_stream(
                    approval.id, ApprovalResumeRequest(), run_id=run_id
                )
            ]

        task = asyncio.create_task(consume())
        assert await asyncio.to_thread(approval_store.started.wait, 3)
        task.cancel()
        approval_store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        refreshed = approval_store.get(approval.id)
        assert refreshed.status == "approved"
        assert refreshed.last_resume_outcome == "cancelled"
        assert not (app_settings.resolved_workspace_dir() / "resumed.txt").exists()
        assert session_store.release_run("cancel-before-tool", run_id) is False

    asyncio.run(exercise())


def test_disconnect_during_model_stream_emits_one_cancelled_terminal(tmp_path: Path) -> None:
    class WaitingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def stream_generate(self, **kwargs):  # noqa: ANN001, ANN201
            del kwargs
            try:
                self.started.set()
                await asyncio.Event().wait()
                yield LLMStreamEvent(result=LLMResult(content="unreachable"))
            finally:
                self.closed = True

    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = ApprovalStore(database)
        session_store.get_or_create("disconnect-model")
        session_store.append_event(
            "disconnect-model", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "disconnect-model")
        llm = WaitingLLM()
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=llm,
        )
        disconnected = False

        async def is_disconnected() -> bool:
            return disconnected

        run_id = controller.acquire_session_run("disconnect-model")

        async def consume() -> list[str]:
            return [
                event
                async for event in controller.handle_approval_resume_stream(
                    approval.id,
                    ApprovalResumeRequest(),
                    is_cancelled=is_disconnected,
                    run_id=run_id,
                )
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(llm.started.wait(), timeout=3)
        disconnected = True
        events = parse_events(await asyncio.wait_for(task, timeout=3))
        terminals = [name for name, _ in events if name in {"done", "error", "cancelled"}]
        assert terminals == ["cancelled"]
        assert llm.closed is True
        assert approval_store.get(approval.id).status == "consumed"
        assert approval_store.get(approval.id).last_resume_outcome is None
        assert session_store.release_run("disconnect-model", run_id) is False

    asyncio.run(exercise())


def test_task_cancel_during_model_stream_preserves_consumed_tool_fact(tmp_path: Path) -> None:
    class WaitingLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def stream_generate(self, **kwargs):  # noqa: ANN001, ANN201
            del kwargs
            try:
                self.started.set()
                await asyncio.Event().wait()
                yield LLMStreamEvent(result=LLMResult(content="unreachable"))
            finally:
                self.closed = True

    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = ApprovalStore(database)
        session_store.get_or_create("cancel-model")
        session_store.append_event(
            "cancel-model", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "cancel-model")
        llm = WaitingLLM()
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=llm,
        )
        run_id = controller.acquire_session_run("cancel-model")

        async def consume() -> list[str]:
            return [
                event
                async for event in controller.handle_approval_resume_stream(
                    approval.id, ApprovalResumeRequest(), run_id=run_id
                )
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(llm.started.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert llm.closed is True
        assert approval_store.get(approval.id).status == "consumed"
        assert approval_store.get(approval.id).last_resume_outcome is None
        assert session_store.release_run("cancel-model", run_id) is False

    asyncio.run(exercise())


def test_task_cancel_during_sync_tool_waits_and_preserves_tool_fact(
    monkeypatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = ApprovalStore(database)
        session_store.get_or_create("cancel-tool")
        session_store.append_event(
            "cancel-tool", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "cancel-tool")
        llm = FinalAsyncLLM()
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=llm,
        )
        started = threading.Event()
        release = threading.Event()
        real_replace = file_tools.os.replace

        def blocking_replace(source, target):  # noqa: ANN001, ANN201
            started.set()
            assert release.wait(timeout=3)
            return real_replace(source, target)

        monkeypatch.setattr(file_tools.os, "replace", blocking_replace)
        run_id = controller.acquire_session_run("cancel-tool")

        async def consume() -> list[str]:
            return [
                event
                async for event in controller.handle_approval_resume_stream(
                    approval.id, ApprovalResumeRequest(), run_id=run_id
                )
            ]

        task = asyncio.create_task(consume())
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        refreshed = approval_store.get(approval.id)
        session = session_store.get("cancel-tool")
        assert refreshed.status == "consumed"
        assert refreshed.last_resume_outcome is None
        assert session is not None and len(session.tool_calls) == 1
        assert session.tool_calls[0].status == "ok"
        assert llm.calls == 0
        assert (app_settings.resolved_workspace_dir() / "resumed.txt").read_text() == "once"
        assert session_store.release_run("cancel-tool", run_id) is False

    asyncio.run(exercise())


def test_disconnect_during_sync_tool_waits_then_emits_cancelled(
    monkeypatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = ApprovalStore(database)
        session_store.get_or_create("disconnect-tool")
        session_store.append_event(
            "disconnect-tool", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "disconnect-tool")
        llm = FinalAsyncLLM()
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=llm,
        )
        started = threading.Event()
        release = threading.Event()
        real_replace = file_tools.os.replace

        def blocking_replace(source, target):  # noqa: ANN001, ANN201
            started.set()
            assert release.wait(timeout=3)
            return real_replace(source, target)

        monkeypatch.setattr(file_tools.os, "replace", blocking_replace)
        disconnected = False

        async def is_disconnected() -> bool:
            return disconnected

        run_id = controller.acquire_session_run("disconnect-tool")

        async def consume() -> list[str]:
            return [
                event
                async for event in controller.handle_approval_resume_stream(
                    approval.id,
                    ApprovalResumeRequest(),
                    is_cancelled=is_disconnected,
                    run_id=run_id,
                )
            ]

        task = asyncio.create_task(consume())
        assert await asyncio.to_thread(started.wait, 3)
        disconnected = True
        release.set()
        events = parse_events(await asyncio.wait_for(task, timeout=3))

        terminals = [name for name, _ in events if name in {"done", "error", "cancelled"}]
        assert terminals == ["cancelled"]
        assert approval_store.get(approval.id).status == "consumed"
        assert approval_store.get(approval.id).last_resume_outcome is None
        session = session_store.get("disconnect-tool")
        assert session is not None and len(session.tool_calls) == 1
        assert llm.calls == 0
        assert session_store.release_run("disconnect-tool", run_id) is False

    asyncio.run(exercise())


def test_async_generator_close_releases_run_without_overwriting_tool_fact(
    tmp_path: Path,
) -> None:
    class DeltaThenWaitLLM:
        def __init__(self) -> None:
            self.closed = False

        async def stream_generate(self, **kwargs):  # noqa: ANN001, ANN201
            del kwargs
            try:
                yield LLMStreamEvent(content_delta="partial")
                await asyncio.Event().wait()
            finally:
                self.closed = True

    async def exercise() -> None:
        app_settings = settings(tmp_path)
        database = app_settings.resolved_sqlite_path()
        session_store = SQLiteSessionStore(database)
        approval_store = ApprovalStore(database)
        session_store.get_or_create("close-generator")
        session_store.append_event(
            "close-generator", "message", {"role": "user", "content": "write"}
        )
        approval = approved_write(approval_store, "close-generator")
        llm = DeltaThenWaitLLM()
        controller = AgentController(
            app_settings=app_settings,
            session_store=session_store,
            approval_store=approval_store,
            llm_client=llm,
        )
        run_id = controller.acquire_session_run("close-generator")
        stream = controller.handle_approval_resume_stream(
            approval.id, ApprovalResumeRequest(), run_id=run_id
        )
        assert parse_events([await anext(stream)])[0][0] == "tool_call"
        assert parse_events([await anext(stream)])[0][0] == "delta"
        await stream.aclose()

        assert approval_store.get(approval.id).status == "consumed"
        assert approval_store.get(approval.id).last_resume_outcome is None
        assert llm.closed is True
        assert session_store.release_run("close-generator", run_id) is False

    asyncio.run(exercise())


def test_old_run_late_cancel_outcome_and_release_are_fenced(tmp_path: Path) -> None:
    database = tmp_path / "fenced-cancel.sqlite3"
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("late-cancel")
    approval = approved_write(approval_store, "late-cancel")
    old_run = session_store.acquire_run("late-cancel", lease_seconds=30)
    assert approval_store.set_resume_outcome_fenced(
        approval.id,
        "cancelled",
        session_id="late-cancel",
        run_id=old_run,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE session_runs SET expires_at = ? WHERE session_id = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), "late-cancel"),
        )
    new_run = session_store.acquire_run("late-cancel", lease_seconds=30)
    assert approval_store.set_resume_outcome_fenced(
        approval.id, None, session_id="late-cancel", run_id=new_run
    )

    assert not approval_store.set_resume_outcome_fenced(
        approval.id,
        "cancelled",
        session_id="late-cancel",
        run_id=old_run,
    )
    assert session_store.release_run("late-cancel", old_run) is False
    assert approval_store.get(approval.id).last_resume_outcome is None
    with sqlite3.connect(database) as connection:
        active = connection.execute(
            "SELECT run_id FROM session_runs WHERE session_id = ?", ("late-cancel",)
        ).fetchone()
    assert active == (new_run,)
    assert session_store.release_run("late-cancel", new_run) is True


def test_command_disabled_after_approval_records_error_but_done_is_valid(tmp_path: Path) -> None:
    app_settings = settings(tmp_path, ALLOW_COMMAND_EXECUTION=True)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("command-disabled")
    session_store.append_event(
        "command-disabled", "message", {"role": "user", "content": "run it"}
    )
    approval = approval_store.create_pending(
        session_id="command-disabled",
        tool_name="run_command",
        arguments={"command": ["python", "--version"], "cwd": ".", "timeout_seconds": 60},
        reason="run command",
    )
    approval_store.set_status(approval.id, "approved")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM("command was not executed"),
    )

    events = collect_resume(
        controller,
        approval.id,
        "command-disabled",
        request_settings=AgentSettings(allow_command_execution=False),
    )

    assert [name for name, _ in events] == ["tool_call", "delta", "done"]
    assert approval_store.get(approval.id).status == "approved"
    session = session_store.get("command-disabled")
    assert session is not None
    assert session.tool_calls[-1].status == "error"
    assert session.tool_calls[-1].arguments["approval_id"] == approval.id


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("missing_tool", {"value": "invalid"}),
        ("write_file", {"content": "missing path"}),
    ],
)
def test_unavailable_or_invalid_approved_tool_records_error_and_continues(
    tmp_path: Path, tool_name: str, arguments: dict[str, Any]
) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create(tool_name)
    session_store.append_event(tool_name, "message", {"role": "user", "content": "run"})
    approval = approval_store.create_pending(
        session_id=tool_name,
        tool_name=tool_name,
        arguments=arguments,
        reason="invalid approved tool",
    )
    approval_store.set_status(approval.id, "approved")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM("final answer after tool error"),
    )

    events = collect_resume(controller, approval.id, tool_name)

    assert events[-1][0] == "done"
    assert events[-1][1]["answer"] == "final answer after tool error"
    assert approval_store.get(approval.id).status == "approved"
    session = session_store.get(tool_name)
    assert session is not None
    assert session.tool_calls[-1].status == "error"


def test_consumed_approval_with_handler_error_persists_uncertain_facts(
    monkeypatch, tmp_path: Path
) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("handler-error")
    session_store.append_event(
        "handler-error", "message", {"role": "user", "content": "write"}
    )
    approval = approved_write(approval_store, "handler-error")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM("final despite handler error"),
    )

    def fail_replace(source, target):  # noqa: ANN001
        del source, target
        raise OSError("simulated handler failure")

    monkeypatch.setattr(file_tools.os, "replace", fail_replace)
    events = collect_resume(controller, approval.id, "handler-error")

    assert events[-1][0] == "done"
    assert approval_store.get(approval.id).status == "consumed"
    session = session_store.get("handler-error")
    assert session is not None
    assert session.tool_calls[-1].status == "error"
    assert session.tool_calls[-1].arguments["approval_id"] == approval.id
    assert not (app_settings.resolved_workspace_dir() / "resumed.txt").exists()


def test_cancelled_resume_outcome_survives_refresh_and_clears_on_retry(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    database = app_settings.resolved_sqlite_path()
    session_store = SQLiteSessionStore(database)
    approval_store = ApprovalStore(database)
    session_store.get_or_create("cancelled-fact")
    session_store.append_event(
        "cancelled-fact", "message", {"role": "user", "content": "write"}
    )
    approval = approved_write(approval_store, "cancelled-fact")
    controller = AgentController(
        app_settings=app_settings,
        session_store=session_store,
        approval_store=approval_store,
        llm_client=FinalAsyncLLM(),
    )

    async def cancel_before_tool() -> list[tuple[str, dict[str, Any]]]:
        async def cancelled() -> bool:
            return True

        run_id = controller.acquire_session_run("cancelled-fact")
        raw = [
            event
            async for event in controller.handle_approval_resume_stream(
                approval.id,
                ApprovalResumeRequest(),
                is_cancelled=cancelled,
                run_id=run_id,
            )
        ]
        return parse_events(raw)

    events = asyncio.run(cancel_before_tool())

    assert [name for name, _ in events] == ["cancelled"]
    assert approval_store.get(approval.id).last_resume_outcome == "cancelled"
    assert approval_store.get(approval.id).status == "approved"
    assert not (app_settings.resolved_workspace_dir() / "resumed.txt").exists()

    retry_events = collect_resume(controller, approval.id, "cancelled-fact")
    assert retry_events[-1][0] == "done"
    refreshed = approval_store.get(approval.id)
    assert refreshed.status == "consumed"
    assert refreshed.last_resume_outcome is None


def test_approval_resume_outcome_migration_is_idempotent_for_legacy_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-approvals.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "session",
                "write_file",
                '{"path":"legacy.txt"}',
                "legacy approval",
                "pending",
                "2099-01-01T00:00:00+00:00",
                "2099-01-01T00:00:00+00:00",
            ),
        )

    first = ApprovalStore(database)
    second = ApprovalStore(database)

    assert first.get("legacy").last_resume_outcome is None
    assert second.get("legacy").last_resume_outcome is None
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(approvals)")}
    assert "last_resume_outcome" in columns
    assert "replacement_approval_id" in columns


def test_approval_mutation_api_returns_stable_structured_errors(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token-that-is-at-least-32-bytes"}

    missing = client.post("/api/approvals/missing/approve", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "approval_not_found"

    expired = app.state.approval_store.create_pending(
        session_id="errors", tool_name="write_file", arguments={"path": "x"}, reason="test"
    )
    with sqlite3.connect(app.state.approval_store.database_path) as connection:
        connection.execute(
            "UPDATE approvals SET expires_at = ? WHERE id = ?",
            ((utc_now() - timedelta(seconds=1)).isoformat(), expired.id),
        )
    expired_response = client.post(
        f"/api/approvals/{expired.id}/reject", headers=headers
    )
    assert expired_response.status_code == 409
    assert expired_response.json()["detail"]["code"] == "approval_expired"

    rejected = app.state.approval_store.create_pending(
        session_id="errors", tool_name="write_file", arguments={"path": "y"}, reason="test"
    )
    app.state.approval_store.set_status(rejected.id, "rejected")
    invalid = client.post(f"/api/approvals/{rejected.id}/approve", headers=headers)
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "approval_invalid_state"

    consumed = app.state.approval_store.create_pending(
        session_id="errors", tool_name="write_file", arguments={"path": "z"}, reason="test"
    )
    with sqlite3.connect(app.state.approval_store.database_path) as connection:
        connection.execute(
            "UPDATE approvals SET status = 'consumed' WHERE id = ?", (consumed.id,)
        )
    consumed_response = client.post(
        f"/api/approvals/{consumed.id}/reject", headers=headers
    )
    assert consumed_response.status_code == 409
    assert consumed_response.json()["detail"]["code"] == "approval_consumed"
