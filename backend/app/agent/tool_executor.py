from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.schemas import ToolCallRecord


@dataclass(slots=True)
class ToolContext:
    workspace_dir: Path
    session_id: str = "tool-context"
    allow_command_execution: bool = False
    approval_store: Any | None = None
    rag_store: Any | None = None
    max_result_chars: int = 20_000
    max_command_output_chars: int = 20_000
    user_id: str = "00000000-0000-0000-0000-000000000001"


class ToolExecutor:
    def __init__(self, registry, context: ToolContext) -> None:
        self.registry = registry
        self.context = context

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        try:
            tool = self.registry.get_tool(name)
            validated = tool.input_model.model_validate(arguments)
            result = self._bounded_json(tool.handler(self.context, validated))
            return ToolCallRecord(name=name, arguments=arguments, status="ok", result=result)
        except ValidationError as exc:
            return ToolCallRecord(
                name=name,
                arguments=arguments,
                status="error",
                error=self._bounded_json(exc.errors()),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolCallRecord(
                name=name,
                arguments=arguments,
                status="error",
                error=self._bounded_json(str(exc)),
            )

    def execute_model_call(self, name: str, arguments: dict[str, Any]) -> ToolCallRecord:
        if "approval_id" in arguments:
            return ToolCallRecord(
                name=name,
                arguments={key: value for key, value in arguments.items() if key != "approval_id"},
                status="error",
                error="approval_id 只能由服务端审批续跑流程注入",
            )
        return self.execute(name, arguments)

    def execute_approved(
        self, name: str, arguments: dict[str, Any], *, approval_id: str
    ) -> ToolCallRecord:
        sanitized = {key: value for key, value in arguments.items() if key != "approval_id"}
        return self.execute(name, {**sanitized, "approval_id": approval_id})

    def _bounded_json(self, value: Any) -> Any:
        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
            normalized = json.loads(serialized)
        except (TypeError, ValueError):
            serialized = json.dumps(str(value), ensure_ascii=False)
            normalized = json.loads(serialized)
        limit = self.context.max_result_chars
        if len(serialized) <= limit:
            return normalized
        wrapper = {
            "truncated": True,
            "original_chars": len(serialized),
            "preview": serialized,
        }
        while len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))) > limit and wrapper["preview"]:
            overflow = len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"))) - limit
            wrapper["preview"] = wrapper["preview"][: max(0, len(wrapper["preview"]) - max(overflow, 1))]
        return wrapper

    @staticmethod
    def result_to_tool_message(call_id: str, record: ToolCallRecord) -> dict[str, Any]:
        payload = record.model_dump()
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(payload, ensure_ascii=False),
        }
