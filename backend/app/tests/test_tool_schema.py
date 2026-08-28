from __future__ import annotations

import json

from app.agent.tool_executor import ToolContext, ToolExecutor
from app.agent.tool_registry import build_default_registry


def test_all_registered_tool_schemas_are_non_strict_json_contracts() -> None:
    registry = build_default_registry()
    schemas = registry.list_tool_schemas()
    assert len(schemas) == len(registry.names())

    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] in registry.names()
        assert isinstance(function["description"], str) and function["description"]
        assert isinstance(function["parameters"], dict)
        assert function.get("strict") is not True
        serialized = json.dumps(schema, ensure_ascii=False)
        assert '"strict": true' not in serialized.lower()
        assert json.loads(serialized) == schema


def test_git_discriminated_union_schema_is_serializable_without_strict() -> None:
    registry = build_default_registry()
    git_schema = next(
        schema for schema in registry.list_tool_schemas() if schema["function"]["name"] == "git_inspect"
    )
    serialized = json.dumps(git_schema, ensure_ascii=False)
    assert "GitStatusOperation" in serialized
    assert "GitDiffOperation" in serialized
    assert "GitLogOperation" in serialized
    assert "GitShowOperation" in serialized
    assert "GitBranchOperation" in serialized
    assert '"strict": true' not in serialized.lower()


def test_tool_executor_still_rejects_invalid_git_arguments(tmp_path) -> None:  # noqa: ANN001
    registry = build_default_registry()
    executor = ToolExecutor(registry, ToolContext(workspace_dir=tmp_path))
    record = executor.execute("git_inspect", {"operation": "branch", "args": ["-D", "main"]})
    assert record.status == "error"
    assert record.result is None
    assert record.error
