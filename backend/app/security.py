from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from typing import AsyncIterator
from uuid import UUID

import httpx
import anyio
from fastapi import Header, HTTPException, Request, status

_current_user: ContextVar[str | None] = ContextVar("current_user", default=None)
TEST_OWNER_USER_ID = "00000000-0000-0000-0000-000000000001"

@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: tuple[str, ...]

def current_user_id() -> str:
    return _current_user.get() or TEST_OWNER_USER_ID

def set_current_user(user_id: str) -> Token:
    return _current_user.set(user_id)

def reset_current_user(token: Token) -> None:
    _current_user.reset(token)

def _error(code: str, message: str = "认证凭据无效或缺失。") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": code, "message": message}, headers={"WWW-Authenticate": "Bearer"})

def require_local_agent_token(request: Request, x_local_agent_token: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.api_auth_token
    if not x_local_agent_token or not hmac.compare_digest(x_local_agent_token, expected):
        raise _error("local_agent_token_invalid", "本地 Agent 凭据无效或缺失。")

class JwksVerifier:
    def __init__(self, *, url: str, issuer: str, audience: str, ttl_seconds: int) -> None:
        self.url, self.issuer, self.audience, self.ttl_seconds = url, issuer, audience, ttl_seconds
        self._keys: dict[str, tuple[int, int]] = {}
        self._expires_at = 0.0
        self._lock = RLock()

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def _reload(self) -> None:
        try:
            response = httpx.get(self.url, timeout=2.0)
            response.raise_for_status()
            payload = response.json()
            keys = {item["kid"]: (int.from_bytes(self._decode(item["n"]), "big"), int.from_bytes(self._decode(item["e"]), "big")) for item in payload.get("keys", []) if item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256"}
            if not keys: raise ValueError("JWKS contains no supported RSA keys")
        except Exception as exc:
            raise _error("jwks_unavailable", "用户认证服务暂时不可用。") from exc
        self._keys = keys
        self._expires_at = time.monotonic() + self.ttl_seconds

    def _key(self, kid: str) -> tuple[int, int]:
        with self._lock:
            reloaded = False
            if time.monotonic() >= self._expires_at:
                self._reload()
                reloaded = True
            key = self._keys.get(kid)
            if key is None and not reloaded:
                self._reload()
                key = self._keys.get(kid)
            if key is None: raise _error("jwt_kid_invalid")
            return key

    def verify(self, token: str) -> Principal:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = json.loads(self._decode(encoded_header)); claims = json.loads(self._decode(encoded_payload))
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str): raise ValueError("unsupported JWT header")
            n, e = self._key(header["kid"]); signature = self._decode(encoded_signature)
            digest = hashlib.sha256(f"{encoded_header}.{encoded_payload}".encode()).digest()
            digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
            size = (n.bit_length() + 7) // 8
            expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
            actual = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
            if not hmac.compare_digest(actual, expected): raise ValueError("bad signature")
            now = int(time.time()); audience = claims.get("aud"); audiences = [audience] if isinstance(audience, str) else audience
            if claims.get("iss") != self.issuer or self.audience not in (audiences or []): raise ValueError("bad issuer or audience")
            if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now: raise ValueError("expired")
            if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now: raise ValueError("not yet valid")
            user_id = str(UUID(claims["sub"])); roles = claims.get("roles", [])
            if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles): raise ValueError("bad roles")
            return Principal(user_id=user_id, roles=tuple(roles))
        except HTTPException: raise
        except Exception as exc: raise _error("user_access_token_invalid") from exc

async def require_user_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AsyncIterator[Principal]:
    require_local_agent_token(request, request.headers.get("x-local-agent-token"))
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token: raise _error("user_access_token_missing")
    principal = await anyio.to_thread.run_sync(request.app.state.jwt_verifier.verify, token)
    request.state.principal = principal
    context_token = set_current_user(principal.user_id)
    try:
        yield principal
    finally:
        reset_current_user(context_token)



def is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def is_bootstrap_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    return bool(origin and origin != "null" and origin in request.app.state.settings.cors_origins())
