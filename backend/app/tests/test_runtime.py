from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import SERVICE_NAME, SERVICE_VERSION
from app.config import AppSettings
from app.main import create_app


def test_runtime_requires_auth_and_returns_only_non_sensitive_identity(tmp_path: Path) -> None:
    token = "runtime-test-token-that-is-long-enough"
    workspace = tmp_path / "workspace"
    database = tmp_path / "private" / "agent.sqlite3"
    secret = "sk-secret-runtime-value"
    app = create_app(
        AppSettings(
            WORKSPACE_DIR=str(workspace),
            SQLITE_PATH=str(database),
            API_AUTH_TOKEN=token,
            OPENAI_API_KEY=secret,
            ALLOW_COMMAND_EXECUTION=False,
        )
    )

    with TestClient(app) as client:
        unauthenticated = client.get("/api/runtime")
        response = client.get(
            "/api/runtime", headers={"Authorization": f"Bearer {token}"}
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "workspace": str(workspace),
        "command_execution_allowed": False,
        "database": {"type": "sqlite", "status": "ready"},
    }
    serialized = response.text
    assert secret not in serialized
    assert token not in serialized
    assert str(database) not in serialized


def test_health_exposes_service_identity_without_auth(tmp_path: Path) -> None:
    app = create_app(
        AppSettings(
            WORKSPACE_DIR=str(tmp_path / "workspace"),
            SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
            API_AUTH_TOKEN="runtime-test-token-that-is-long-enough",
        )
    )
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
    }
