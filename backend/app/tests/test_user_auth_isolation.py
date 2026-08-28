from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.security import JwksVerifier, reset_current_user, set_current_user
from app.services.approval_store import ApprovalStore
from app.services.collaboration_store import CollaborationStore
from app.services.rag_store import RagStore
from app.services.session_store import SQLiteSessionStore
from app.services.stream_lease import StreamRunLeaseGuard
from app.models.schemas import CollaborationAgentCreate, CollaborationCreateRequest
from app.agent.tool_executor import ToolContext
from app.tools.rag_tools import IndexWorkspaceArgs, QueryKnowledgeArgs, index_workspace, query_knowledge

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"

def owner(user_id: str):
    class Scope:
        def __enter__(self): self.token = set_current_user(user_id)
        def __exit__(self, *_): reset_current_user(self.token)
    return Scope()

def unsigned_token(claims: dict, kid="missing") -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{encode({'alg':'RS256','kid':kid})}.{encode(claims)}.{encode({'bad':True})}"

def _prime(bits: int) -> int:
    def probable(value: int) -> bool:
        small = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
        if any(value % divisor == 0 for divisor in small):
            return value in small
        d, power = value - 1, 0
        while d % 2 == 0: power += 1; d //= 2
        for base in (2, 3, 5, 7, 11, 13, 17):
            if base >= value: continue
            x = pow(base, d, value)
            if x in (1, value - 1): continue
            for _ in range(power - 1):
                x = pow(x, 2, value)
                if x == value - 1: break
            else: return False
        return True
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if probable(candidate): return candidate

def _rsa_fixture():
    exponent = 65537
    while True:
        p, q = _prime(256), _prime(256)
        if p != q and math.gcd(exponent, (p - 1) * (q - 1)) == 1: break
    modulus = p * q
    private = pow(exponent, -1, (p - 1) * (q - 1))
    encode_int = lambda value: base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
    return modulus, private, {"kid":"test-key","kty":"RSA","alg":"RS256","n":encode_int(modulus),"e":encode_int(exponent)}

def _signed_token(claims: dict, modulus: int, private: int, kid="test-key") -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    header, payload = encode({"alg":"RS256","kid":kid}), encode(claims)
    digest = hashlib.sha256(f"{header}.{payload}".encode()).digest()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    size = (modulus.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private, modulus).to_bytes(size, "big")
    return f"{header}.{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

def test_user_api_requires_both_local_and_user_tokens(tmp_path: Path):
    settings = AppSettings(
        WORKSPACE_DIR=str(tmp_path / "workspace"),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        API_AUTH_TOKEN="local-token-that-is-at-least-32-bytes",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/sessions", headers={"X-Local-Agent-Token":settings.api_auth_token}).status_code == 401
        assert client.get("/api/sessions", headers={"Authorization":"Bearer test-user-jwt"}).status_code == 401
        assert client.get("/api/runtime", headers={"X-Local-Agent-Token":settings.api_auth_token}).status_code == 200

def test_jwt_rejects_bad_signature_issuer_audience_expiry_and_subject(monkeypatch):
    modulus, private, jwk = _rsa_fixture()
    class Response:
        def raise_for_status(self): return None
        def json(self): return {"keys":[jwk]}
    monkeypatch.setattr("httpx.get", lambda *_args, **_kwargs: Response())
    verifier = JwksVerifier(url="http://jwks.test/keys", issuer="issuer", audience="aud", ttl_seconds=30)
    base = {"iss":"issuer","aud":"aud","sub":USER_A,"exp":int(time.time()) + 60,"roles":["USER"]}
    valid = _signed_token(base, modulus, private)
    signature_head, signature_tail = valid.rsplit(".", 1)
    tampered = ("A" if signature_tail[0] != "A" else "B") + signature_tail[1:]
    invalid = [
        f"{signature_head}.{tampered}",
        _signed_token({**base,"iss":"other"}, modulus, private),
        _signed_token({**base,"aud":"other"}, modulus, private),
        _signed_token({**base,"exp":int(time.time()) - 1}, modulus, private),
        _signed_token({**base,"sub":"not-a-uuid"}, modulus, private),
    ]
    for token in invalid:
        with pytest.raises(HTTPException) as exc:
            verifier.verify(token)
        assert exc.value.detail == {"code":"user_access_token_invalid","message":"认证凭据无效或缺失。"}

def test_jwt_failures_are_stable_and_jwks_outage_fails_closed(monkeypatch):
    verifier = JwksVerifier(url="http://unavailable.test/jwks", issuer="issuer", audience="aud", ttl_seconds=30)
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(HTTPException) as exc:
        verifier.verify(unsigned_token({"iss":"issuer","aud":"aud","sub":USER_A,"exp":9999999999,"roles":["USER"]}))
    assert exc.value.detail == {"code":"jwks_unavailable","message":"用户认证服务暂时不可用。"}

def test_unknown_kid_reloads_jwks_only_once(monkeypatch):
    verifier = JwksVerifier(url="http://jwks.test/keys", issuer="issuer", audience="aud", ttl_seconds=30)
    calls = 0
    class Response:
        def raise_for_status(self): return None
        def json(self):
            return {"keys":[{"kid":"known","kty":"RSA","alg":"RS256","n":"AQAB","e":"AQAB"}]}
    def get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()
    monkeypatch.setattr("httpx.get", get)
    with pytest.raises(HTTPException) as exc:
        verifier.verify(unsigned_token({"iss":"issuer","aud":"aud","sub":USER_A,"exp":9999999999,"roles":["USER"]}, kid="unknown"))
    assert exc.value.detail["code"] == "jwt_kid_invalid"
    assert calls == 1

def test_sessions_approvals_collaborations_rag_and_workspace_are_isolated(tmp_path: Path):
    database = tmp_path / "agent.sqlite3"; workspace = tmp_path / "workspace"
    sessions = SQLiteSessionStore(database); approvals = ApprovalStore(database); collaborations = CollaborationStore(database); rag = RagStore(database, workspace)
    payload = CollaborationCreateRequest(session_id="session-a", title="room", rounds=1, agents=[
        CollaborationAgentCreate(id="a",name="A",role="Coordinator",position=0,is_coordinator=True),
        CollaborationAgentCreate(id="b",name="B",role="Member",position=1,is_coordinator=False),
    ])
    with owner(USER_A):
        sessions.get_or_create("session-a")
        approval = approvals.create_pending(session_id="session-a",tool_name="write_file",arguments={"path":"a"},reason="test")
        room = collaborations.create(payload)
        user_workspace = AppSettings(WORKSPACE_DIR=str(workspace)).resolved_user_workspace_dir(USER_A)
        (user_workspace / "note.txt").write_text("alpha", encoding="utf-8")
        user_rag = RagStore(database, user_workspace); user_rag.index_workspace()
        assert user_rag.search("alpha")["total_matches"] == 1
    with owner(USER_B):
        assert sessions.get("session-a") is None
        assert approvals.get(approval.id) is None
        assert collaborations.get(room.id) is None
        assert AppSettings(WORKSPACE_DIR=str(workspace)).resolved_user_workspace_dir(USER_B) != user_workspace
        assert RagStore(database, AppSettings(WORKSPACE_DIR=str(workspace)).resolved_user_workspace_dir(USER_B)).search("alpha")["total_matches"] == 0

def test_legacy_null_owner_rows_are_hidden_from_real_users(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"; store = SQLiteSessionStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sessions (session_id,owner_user_id,created_at,updated_at,messages_json,tool_calls_json,display_title) VALUES ('legacy',NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','[]','[]','legacy')")
    with owner(USER_A): assert store.get("legacy") is None

def test_stream_lease_release_keeps_verified_owner_across_worker_thread(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "lease.sqlite3")
    with owner(USER_A):
        store.get_or_create("session-a")
        run_id = store.acquire_run("session-a", lease_seconds=30)
        guard = StreamRunLeaseGuard(store, "session-a", run_id)
        assert guard.close_sync() is True
    assert guard._release_done.wait(timeout=3)
    assert guard._release_error is None
    assert guard._release_result is True

def test_production_rag_assembly_cannot_index_another_users_workspace(tmp_path: Path):
    settings = AppSettings(
        WORKSPACE_DIR=str(tmp_path / "workspace"),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        API_AUTH_TOKEN="local-token-that-is-at-least-32-bytes",
    )
    app = create_app(settings)
    workspace_a = settings.resolved_user_workspace_dir(USER_A)
    workspace_b = settings.resolved_user_workspace_dir(USER_B)
    (workspace_a / "public.txt").write_text("alpha owner a", encoding="utf-8")
    (workspace_b / "private.txt").write_text("bravo private owner b", encoding="utf-8")

    with owner(USER_A):
        context = ToolContext(
            workspace_dir=workspace_a,
            user_id=USER_A,
            rag_store=app.state.rag_store,
        )
        index_workspace(context, IndexWorkspaceArgs())
        result = query_knowledge(context, QueryKnowledgeArgs(query="bravo"))

    assert result["total_matches"] == 0
    assert all(USER_B not in match["path"] for match in result["matches"])

def test_upgrade_removes_legacy_cross_user_rag_pollution(tmp_path: Path):
    database = tmp_path / "legacy-rag.sqlite3"
    SQLiteSessionStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO rag_documents (owner_user_id,path,content,updated_at) VALUES (?,?,?,?)",
            (USER_A, f"users/{USER_B}/private.txt", "bravo historical secret", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO rag_documents (owner_user_id,path,content,updated_at) VALUES (?,?,?,?)",
            (USER_A, "notes/legitimate.txt", "alpha legitimate", "2026-01-01T00:00:00+00:00"),
        )

    store = RagStore(database, tmp_path / "workspace")
    with owner(USER_A):
        assert store.search("bravo")["total_matches"] == 0
        assert store.search("alpha")["total_matches"] == 1
