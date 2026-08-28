from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.security import Principal


@pytest.fixture(autouse=True)
def legacy_http_auth_compat(monkeypatch):
    """Adapt pre-user-auth API tests without weakening the production boundary."""
    original = TestClient.request

    def request(self, method, url, **kwargs):  # noqa: ANN001
        headers = dict(kwargs.pop("headers", {}) or {})
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer test-token") or authorization.startswith("Bearer runtime-test"):
            headers["X-Local-Agent-Token"] = authorization.removeprefix("Bearer ")
            headers["Authorization"] = "Bearer test-user-jwt"
        return original(self, method, url, headers=headers, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)
    original_verify = __import__("app.security", fromlist=["JwksVerifier"]).JwksVerifier.verify
    monkeypatch.setattr("app.security.JwksVerifier.verify", lambda self, token: Principal("00000000-0000-0000-0000-000000000001", ("USER",)) if token == "test-user-jwt" else original_verify(self, token))
