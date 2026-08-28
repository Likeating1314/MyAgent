from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.models.schemas import ToolCallRecord


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ConversationMemory:
    session_id: str
    display_title: str | None = None
    archived_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.display_title is None:
            self.display_title = self.session_id

    def touch(self) -> None:
        self.updated_at = utc_now()
