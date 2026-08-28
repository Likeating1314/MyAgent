from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.controller import AgentController
from app.agent.tool_executor import ToolContext
from app.config import AppSettings
from app.main import create_app
from app.models.schemas import AgentSettings
from app.services.approval_store import ApprovalStore
from app.tools.command_tools import RunCommandArgs, run_command
from app.tools.file_tools import WriteFileArgs, write_file


def make_settings(tmp_path: Path, *, allow_commands: bool = False) -> AppSettings:
    return AppSettings(
        WORKSPACE_DIR=str(tmp_path / "workspace"),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        ALLOW_COMMAND_EXECUTION=allow_commands,
        API_AUTH_TOKEN="test-token-that-is-at-least-32-bytes",
    )


def test_client_cannot_enable_server_disabled_commands(tmp_path: Path) -> None:
    controller = AgentController(app_settings=make_settings(tmp_path, allow_commands=False))
    merged = controller._merge_settings(AgentSettings(allow_command_execution=True))
    assert merged.allow_command_execution is False


def test_client_can_only_disable_server_enabled_commands(tmp_path: Path) -> None:
    controller = AgentController(app_settings=make_settings(tmp_path, allow_commands=True))
    assert controller._merge_settings(AgentSettings(allow_command_execution=False)).allow_command_execution is False
    assert controller._merge_settings(AgentSettings(allow_command_execution=True)).allow_command_execution is True


def test_api_routes_require_token_but_health_is_public(tmp_path: Path) -> None:
    client = TestClient(create_app(make_settings(tmp_path)))
    assert client.get("/health").status_code == 200
    protected_requests = [
        ("GET", "/api/tools", None),
        ("GET", "/api/sessions", None),
        ("POST", "/api/sessions", {}),
        ("GET", "/api/approvals", None),
        ("POST", "/api/approvals/missing/approve", None),
        ("POST", "/api/chat", {"session_id": "s1", "message": "hello"}),
        ("POST", "/api/chat/stream", {"session_id": "s1", "message": "hello"}),
    ]
    for method, path, payload in protected_requests:
        assert client.request(method, path, json=payload).status_code == 401
    assert client.get("/api/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get(
        "/api/tools",
        headers={"Authorization": "Bearer test-token-that-is-at-least-32-bytes"},
    )
    assert response.status_code == 200


def test_local_trusted_frontend_can_bootstrap_token(tmp_path: Path) -> None:
    client = TestClient(create_app(make_settings(tmp_path)), client=("127.0.0.1", 50123))
    response = client.get("/auth/token", headers={"Origin": "http://127.0.0.1:5173"})
    assert response.status_code == 200
    assert response.json() == {"token": "test-token-that-is-at-least-32-bytes"}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/auth/token", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/auth/token", headers={"Origin": "null"}).status_code == 403


def test_non_loopback_bootstrap_requires_explicit_server_setting(tmp_path: Path) -> None:
    default_client = TestClient(create_app(make_settings(tmp_path)), client=("172.18.0.1", 50123))
    headers = {"Origin": "http://127.0.0.1:5173"}
    assert default_client.get("/auth/token", headers=headers).status_code == 403

    docker_settings = make_settings(tmp_path)
    docker_settings.allow_non_loopback_token_bootstrap = True
    enabled_client = TestClient(create_app(docker_settings), client=("172.18.0.1", 50123))
    assert enabled_client.get("/auth/token", headers=headers).status_code == 200


def test_cors_default_rejects_untrusted_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(make_settings(tmp_path)))
    trusted = client.options(
        "/api/tools",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert trusted.status_code == 200
    assert trusted.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    untrusted = client.options(
        "/api/tools",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert untrusted.status_code == 400
    assert "access-control-allow-origin" not in untrusted.headers


def create_command_approval(store: ApprovalStore, context: ToolContext, args: RunCommandArgs):
    with pytest.raises(PermissionError):
        run_command(context, args)
    approval = store.list_requests(status="pending")[0]
    store.set_status(approval.id, "approved")
    return approval


def test_approval_cannot_cross_sessions_or_change_arguments(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(tmp_path, "session-a", True, store)
    args = RunCommandArgs(command=[sys.executable, "--version"])
    approval = create_command_approval(store, context, args)

    other_session = ToolContext(tmp_path, "session-b", True, store)
    with pytest.raises(PermissionError):
        run_command(other_session, args.model_copy(update={"approval_id": approval.id}))
    assert store.get(approval.id).status == "approved"

    changed = RunCommandArgs(command=[sys.executable, "-c", "print('changed')"], approval_id=approval.id)
    with pytest.raises(PermissionError):
        run_command(context, changed)
    assert store.get(approval.id).status == "approved"


def test_approval_is_consumed_once(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(tmp_path, "session-a", True, store)
    args = RunCommandArgs(command=[sys.executable, "--version"])
    approval = create_command_approval(store, context, args)
    approved_args = args.model_copy(update={"approval_id": approval.id})

    assert run_command(context, approved_args)["returncode"] == 0
    assert store.get(approval.id).status == "consumed"
    with pytest.raises(PermissionError):
        run_command(context, approved_args)


def test_approval_consumption_is_atomic(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    arguments = {"command": ["python", "--version"], "cwd": ".", "timeout_seconds": 60}
    approval = store.create_pending(
        session_id="session-a", tool_name="run_command", arguments=arguments, reason="test"
    )
    store.set_status(approval.id, "approved")

    def consume() -> bool:
        return store.consume_approved(
            approval.id, session_id="session-a", tool_name="run_command", arguments=arguments
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]
    assert store.get(approval.id).status == "consumed"


def test_expired_approval_cannot_be_consumed(tmp_path: Path) -> None:
    database_path = tmp_path / "approvals.sqlite3"
    store = ApprovalStore(database_path)
    arguments = {"path": "a.txt", "content": "a", "overwrite": True, "expected_sha256": None}
    approval = store.create_pending(session_id="s1", tool_name="write_file", arguments=arguments, reason="test")
    store.set_status(approval.id, "approved")
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", approval.id))
    assert not store.consume_approved(
        approval.id, session_id="s1", tool_name="write_file", arguments=arguments
    )
    assert store.get(approval.id).status == "approved"


def test_write_file_requires_approval_and_exposes_diff(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(tmp_path, "session-a", False, store)
    target = tmp_path / "notes.txt"
    args = WriteFileArgs(path="notes.txt", content="hello\n")

    with pytest.raises(PermissionError):
        write_file(context, args)
    assert not target.exists()
    approval = store.list_requests(status="pending")[0]
    assert approval.details["path"] == "notes.txt"
    assert approval.details["change_type"] == "create"
    assert "+hello" in approval.details["diff"]

    store.set_status(approval.id, "approved")
    result = write_file(context, args.model_copy(update={"approval_id": approval.id}))
    assert result["overwritten"] is False
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert store.get(approval.id).status == "consumed"


def test_write_approval_rejects_tampering_and_stale_target(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(tmp_path, "session-a", False, store)
    target = tmp_path / "notes.txt"
    target.write_text("old\n", encoding="utf-8")
    args = WriteFileArgs(path="notes.txt", content="new\n")
    with pytest.raises(PermissionError):
        write_file(context, args)
    approval = store.list_requests(status="pending")[0]
    store.set_status(approval.id, "approved")

    with pytest.raises(PermissionError):
        write_file(context, WriteFileArgs(path="notes.txt", content="tampered\n", approval_id=approval.id))
    assert target.read_text(encoding="utf-8") == "old\n"
    target.write_text("changed elsewhere\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        write_file(context, args.model_copy(update={"approval_id": approval.id}))
    assert target.read_text(encoding="utf-8") == "changed elsewhere\n"
    assert store.get(approval.id).status == "approved"


def test_write_path_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(workspace, "session-a", False, store)
    with pytest.raises(ValueError, match="工作目录"):
        write_file(context, WriteFileArgs(path="../escape.txt", content="no"))
    with pytest.raises(ValueError, match="工作目录"):
        write_file(context, WriteFileArgs(path=str(tmp_path / "absolute.txt"), content="no"))


def test_write_path_cannot_escape_through_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建测试软链接")
    store = ApprovalStore(tmp_path / "approvals.sqlite3")
    context = ToolContext(workspace, "session-a", False, store)
    with pytest.raises(ValueError, match="工作目录"):
        write_file(context, WriteFileArgs(path="link/escape.txt", content="no"))
    assert not (outside / "escape.txt").exists()
