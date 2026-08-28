from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    workspace_dir: str = Field(default="./workspace", alias="WORKSPACE_DIR")
    sqlite_path: str = Field(default="./data/agent.sqlite3", alias="SQLITE_PATH")
    allow_command_execution: bool = Field(default=False, alias="ALLOW_COMMAND_EXECUTION")
    max_agent_steps: int = Field(default=8, alias="MAX_AGENT_STEPS", ge=1, le=32)
    api_cors_origins: str = Field(
        default="http://127.0.0.1:5173,http://localhost:5173,null",
        alias="API_CORS_ORIGINS",
    )
    api_auth_token: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        alias="API_AUTH_TOKEN",
        min_length=32,
    )
    jwks_url: str = Field(default="http://127.0.0.1:8081/.well-known/jwks.json", alias="JWKS_URL")
    jwt_issuer: str = Field(default="http://127.0.0.1:8081", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="local-agent", alias="JWT_AUDIENCE")
    jwks_cache_seconds: int = Field(default=300, alias="JWKS_CACHE_SECONDS", ge=10, le=86_400)
    allow_non_loopback_token_bootstrap: bool = Field(
        default=False,
        alias="ALLOW_NON_LOOPBACK_TOKEN_BOOTSTRAP",
    )
    approval_ttl_seconds: int = Field(default=900, alias="APPROVAL_TTL_SECONDS", ge=30, le=86_400)
    session_run_lease_seconds: int = Field(
        default=120, alias="SESSION_RUN_LEASE_SECONDS", ge=5, le=3_600
    )
    max_context_chars: int = Field(default=120_000, alias="MAX_CONTEXT_CHARS", ge=1_000)
    max_tool_result_chars: int = Field(default=20_000, alias="MAX_TOOL_RESULT_CHARS", ge=256)
    max_command_output_chars: int = Field(
        default=20_000, alias="MAX_COMMAND_OUTPUT_CHARS", ge=256
    )
    max_user_message_chars: int = Field(
        default=20_000, alias="MAX_USER_MESSAGE_CHARS", ge=1
    )
    max_message_chars: int = Field(default=40_000, alias="MAX_MESSAGE_CHARS", ge=256)

    def resolved_workspace_dir(self) -> Path:
        workspace = Path(self.workspace_dir)
        if not workspace.is_absolute():
            workspace = (REPO_ROOT / workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolved_user_workspace_dir(self, user_id: str) -> Path:
        from uuid import UUID
        validated = str(UUID(user_id))
        root = self.resolved_workspace_dir().resolve()
        if validated == "00000000-0000-0000-0000-000000000001":
            return root
        workspace = (root / "users" / validated).resolve()
        if root not in workspace.parents:
            raise ValueError("用户工作区路径无效")
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def resolved_sqlite_path(self) -> Path:
        database_path = Path(self.sqlite_path)
        if not database_path.is_absolute():
            database_path = (REPO_ROOT / database_path).resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return database_path

    def cors_origins(self) -> list[str]:
        if self.api_cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
