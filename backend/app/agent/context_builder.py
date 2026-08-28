from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.agent.prompts import SYSTEM_PROMPT

OMISSION_MARKER = "[较早历史已省略，以满足上下文预算。]"


class ContextBudgetError(ValueError):
    pass


@dataclass(slots=True)
class _MessageGroup:
    indexes: tuple[int, ...]
    messages: list[dict[str, Any]]


class ContextBuilder:
    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars

    def build(
        self,
        messages: list[dict[str, Any]],
        *,
        current_user_index: int,
    ) -> list[dict[str, Any]]:
        system = {"role": "system", "content": SYSTEM_PROMPT}
        marker = {"role": "system", "content": OMISSION_MARKER}
        groups, malformed_omitted = self._group_messages(messages)
        current_group_index = next(
            (index for index, group in enumerate(groups) if current_user_index in group.indexes),
            None,
        )
        if current_group_index is None:
            raise ContextBudgetError("当前用户消息不在可用上下文中")

        complete = [system, *(message for group in groups for message in group.messages)]
        if not malformed_omitted and self.serialized_chars(complete) <= self.max_chars:
            return complete

        selected = {current_group_index}
        required = [system, marker, *groups[current_group_index].messages]
        if self.serialized_chars(required) > self.max_chars:
            raise ContextBudgetError("system prompt 与当前用户消息超过上下文预算")

        for index in range(len(groups) - 1, -1, -1):
            if index in selected:
                continue
            trial_indexes = sorted((*selected, index))
            trial = [
                system,
                marker,
                *(message for item in trial_indexes for message in groups[item].messages),
            ]
            if self.serialized_chars(trial) <= self.max_chars:
                selected.add(index)

        return [
            system,
            marker,
            *(message for index in sorted(selected) for message in groups[index].messages),
        ]

    @staticmethod
    def serialized_chars(messages: list[dict[str, Any]]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    @staticmethod
    def _group_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[_MessageGroup], bool]:
        groups: list[_MessageGroup] = []
        malformed_omitted = False
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                tool_calls = message.get("tool_calls")
                call_ids = {
                    item.get("id")
                    for item in tool_calls
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                grouped = [message]
                indexes = [index]
                responses: set[str] = set()
                cursor = index + 1
                while cursor < len(messages) and messages[cursor].get("role") == "tool":
                    tool_message = messages[cursor]
                    call_id = tool_message.get("tool_call_id")
                    if call_id not in call_ids:
                        break
                    grouped.append(tool_message)
                    indexes.append(cursor)
                    responses.add(call_id)
                    cursor += 1
                if call_ids and responses == call_ids:
                    groups.append(_MessageGroup(tuple(indexes), grouped))
                else:
                    malformed_omitted = True
                index = cursor
                continue
            if message.get("role") == "tool":
                malformed_omitted = True
                index += 1
                continue
            groups.append(_MessageGroup((index,), [message]))
            index += 1
        return groups, malformed_omitted
