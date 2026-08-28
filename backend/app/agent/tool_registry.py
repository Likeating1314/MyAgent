from __future__ import annotations

from dataclasses import dataclass
import copy
from typing import Any, Callable

from pydantic import BaseModel

from app.agent.tool_executor import ToolContext
from app.tools.command_tools import RunCommandArgs, run_command
from app.tools.file_tools import ListFilesArgs, ReadFileArgs, WriteFileArgs, list_files, read_file, write_file
from app.tools.git_tools import GitInspectArgs, git_inspect
from app.tools.rag_tools import IndexWorkspaceArgs, QueryKnowledgeArgs, index_workspace, query_knowledge
from app.tools.search_tools import SearchTextArgs, search_text


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], Any]

    def schema(self) -> dict[str, Any]:
        parameters = copy.deepcopy(self.input_model.model_json_schema())
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            properties.pop("approval_id", None)
        required = parameters.get("required")
        if isinstance(required, list):
            parameters["required"] = [item for item in required if item != "approval_id"]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolDefinition:
        if name not in self._tools:
            raise KeyError(f"未知工具: {name}")
        return self._tools[name]

    def list_tool_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_file",
            description="读取工作目录内的文本文件内容。",
            input_model=ReadFileArgs,
            handler=read_file,
        )
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description="写入工作目录内的文本文件，支持覆盖控制。",
            input_model=WriteFileArgs,
            handler=write_file,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_files",
            description="列出工作目录内的文件和目录，自动忽略常见缓存目录。",
            input_model=ListFilesArgs,
            handler=list_files,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_text",
            description="在工作目录内按关键词搜索文本。",
            input_model=SearchTextArgs,
            handler=search_text,
        )
    )
    registry.register(
        ToolDefinition(
            name="run_command",
            description="在允许时运行白名单命令，并返回 stdout、stderr 和退出码。",
            input_model=RunCommandArgs,
            handler=run_command,
        )
    )
    registry.register(
        ToolDefinition(
            name="index_workspace",
            description="把工作目录中的文本文件索引到本地 RAG 知识库。",
            input_model=IndexWorkspaceArgs,
            handler=index_workspace,
        )
    )
    registry.register(
        ToolDefinition(
            name="query_knowledge",
            description="从本地 RAG 知识库按关键词检索相关片段。",
            input_model=QueryKnowledgeArgs,
            handler=query_knowledge,
        )
    )
    registry.register(
        ToolDefinition(
            name="git_inspect",
            description="执行结构化只读 Git 检查；按 operation 使用 status、diff、log、show 或 branch 的受限字段。",
            input_model=GitInspectArgs,
            handler=git_inspect,
        )
    )
    return registry
