from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.tool_executor import ToolContext


class IndexWorkspaceArgs(BaseModel):
    max_files: int = Field(default=300, ge=1, le=2_000)
    max_chars_per_file: int = Field(default=80_000, ge=1_000, le=500_000)


class QueryKnowledgeArgs(BaseModel):
    query: str
    max_results: int = Field(default=8, ge=1, le=50)


def index_workspace(context: ToolContext, args: IndexWorkspaceArgs) -> dict[str, Any]:
    if context.rag_store is None:
        raise RuntimeError("RAG 知识库未初始化")
    store = context.rag_store.scoped_to(context.workspace_dir)
    return store.index_workspace(max_files=args.max_files, max_chars_per_file=args.max_chars_per_file)


def query_knowledge(context: ToolContext, args: QueryKnowledgeArgs) -> dict[str, Any]:
    if context.rag_store is None:
        raise RuntimeError("RAG 知识库未初始化")
    store = context.rag_store.scoped_to(context.workspace_dir)
    return store.search(args.query, max_results=args.max_results)
