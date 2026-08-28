from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from uuid import uuid4


class AgentSettings(BaseModel):
    api_provider: str = "openai"
    api_key: str = ""
    model: str = "gpt-4.1-mini"
    api_base_url: str = "https://api.openai.com/v1"
    allow_command_execution: bool = False
    max_agent_steps: int = Field(default=8, ge=1, le=32)
    use_streaming: bool = True


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str
    settings: AgentSettings | None = None


class ApprovalResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: AgentSettings | None = None


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error"] = "ok"
    result: Any | None = None
    error: Any | None = None


class MessageSchema(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Any
    name: str | None = None
    tool_call_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    messages: list[MessageSchema] = Field(default_factory=list)


class SessionInfo(BaseModel):
    session_id: str
    display_title: str
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageSchema] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class SessionSummary(BaseModel):
    session_id: str
    display_title: str
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    tool_call_count: int = 0


class SessionRenameRequest(BaseModel):
    display_title: str


class ApprovalRequest(BaseModel):
    id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected", "consumed"] = "pending"
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    last_resume_outcome: Literal["cancelled"] | None = None
    replacement_approval_id: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str
    version: str


class RuntimeInfo(BaseModel):
    service: str
    version: str
    workspace: str
    command_execution_allowed: bool
    database: dict[str, str]


class CollaborationAgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    role: str = Field(min_length=1, max_length=80)
    prompt: str = Field(default="", max_length=4_000)
    position: int = Field(ge=0, le=4)
    is_coordinator: bool = False


class CollaborationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="多 Agent 协作", min_length=1, max_length=80)
    rounds: int = Field(default=2, ge=1, le=2)
    agents: list[CollaborationAgentCreate] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def validate_agents(self) -> "CollaborationCreateRequest":
        if sum(agent.is_coordinator for agent in self.agents) != 1:
            raise ValueError("协作房间必须且只能有一个 coordinator")
        if len({agent.id for agent in self.agents}) != len(self.agents):
            raise ValueError("Agent id 不能重复")
        if len({agent.position for agent in self.agents}) != len(self.agents):
            raise ValueError("Agent position 不能重复")
        return self


class CollaborationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=20_000)
    settings: AgentSettings | None = None


class CollaborationAgentInfo(CollaborationAgentCreate):
    pass


class CollaborationRunInfo(BaseModel):
    id: str
    collaboration_id: str
    user_message: str
    status: Literal["running", "done", "error", "cancelled"]
    fencing_token: int
    terminal_event: Literal["done", "error", "cancelled"] | None = None
    created_at: datetime
    updated_at: datetime


class CollaborationEventInfo(BaseModel):
    collaboration_id: str
    sequence: int
    run_id: str
    event: str
    agent_id: str | None = None
    message_id: str | None = None
    round: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CollaborationInfo(BaseModel):
    id: str
    session_id: str
    title: str
    rounds: int
    created_at: datetime
    updated_at: datetime
    agents: list[CollaborationAgentInfo] = Field(default_factory=list)
    runs: list[CollaborationRunInfo] = Field(default_factory=list)
    events: list[CollaborationEventInfo] = Field(default_factory=list)


class CollaborationSummary(BaseModel):
    id: str
    session_id: str
    title: str
    rounds: int
    agent_count: int
    active_run_id: str | None = None
    created_at: datetime
    updated_at: datetime
