from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.agent.controller import AgentController
import app.agent.llm_client as llm_module
from app.agent.llm_client import LLMResult
from app.agent.memory import ConversationMemory
from app.agent.tool_executor import ToolContext
from app.config import AppSettings
from app.models.schemas import ChatRequest
from app.services.approval_store import ApprovalStore
from app.services.rag_store import RagStore
from app.services.session_store import SQLiteSessionStore
from app.tools.command_tools import RunCommandArgs, run_command
from app.tools.rag_tools import IndexWorkspaceArgs, QueryKnowledgeArgs, index_workspace, query_knowledge


class StaticLLMClient:
    def generate(self, **kwargs):  # noqa: ANN003
        return LLMResult(content="这是流式结果。", tool_calls=[])


def init_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "agent.sqlite3"
    SQLiteSessionStore(database_path)
    return database_path


def test_sqlite_session_store_persists_messages(tmp_path: Path) -> None:
    database_path = init_database(tmp_path)
    store = SQLiteSessionStore(database_path)
    session = ConversationMemory(session_id="persisted")
    session.messages.append({"role": "user", "content": "你好"})
    store.save(session)

    reloaded = SQLiteSessionStore(database_path).get("persisted")
    assert reloaded is not None
    assert reloaded.messages[0]["content"] == "你好"
    assert [item.session_id for item in store.list_sessions()] == ["persisted"]


def test_rag_index_and_query_knowledge(tmp_path: Path) -> None:
    database_path = init_database(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("本地知识库可以检索 Agent 设计资料。", encoding="utf-8")
    context = ToolContext(workspace_dir=workspace, rag_store=RagStore(database_path, workspace))

    index_result = index_workspace(context, IndexWorkspaceArgs())
    query_result = query_knowledge(context, QueryKnowledgeArgs(query="Agent"))

    assert index_result["indexed"] == 1
    assert query_result["matches"][0]["path"] == "notes.md"


def test_run_command_requires_approval_when_store_is_present(tmp_path: Path) -> None:
    database_path = init_database(tmp_path)
    approval_store = ApprovalStore(database_path)
    context = ToolContext(workspace_dir=tmp_path, allow_command_execution=True, approval_store=approval_store)
    args = RunCommandArgs(command=[sys.executable, "--version"])

    with pytest.raises(PermissionError):
        run_command(context, args)

    pending = approval_store.list_requests(status="pending")
    assert len(pending) == 1
    approval_store.set_status(pending[0].id, "approved")

    result = run_command(context, RunCommandArgs(command=[sys.executable, "--version"], approval_id=pending[0].id))
    assert result["returncode"] == 0
    assert approval_store.get(pending[0].id).status == "consumed"


def test_controller_stream_emits_delta_and_done(tmp_path: Path) -> None:
    settings = AppSettings(
        OPENAI_API_KEY="",
        OPENAI_BASE_URL="https://api.openai.com/v1",
        OPENAI_MODEL="gpt-4.1-mini",
        WORKSPACE_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "agent.sqlite3"),
        ALLOW_COMMAND_EXECUTION=False,
        MAX_AGENT_STEPS=4,
    )
    controller = AgentController(app_settings=settings, llm_client=StaticLLMClient())

    async def collect() -> list[str]:
        return [event async for event in controller.handle_chat_stream(ChatRequest(session_id="s1", message="你好"))]

    events = asyncio.run(collect())
    assert any("event: delta" in event for event in events)
    assert events[-1].startswith("event: done")


def test_llm_client_uses_request_api_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeMessage:
        content = "来自自定义模型"
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.chat = FakeChat()

    monkeypatch.setattr(llm_module, "OpenAI", FakeOpenAI)
    client = llm_module.LLMClient(api_key="", base_url="https://api.openai.com/v1")
    result = client.generate(
        messages=[{"role": "user", "content": "你好"}],
        tools=[],
        settings=llm_module.AgentSettings(
            api_key="user-key",
            api_base_url="https://example.test/v1",
            model="custom-model",
        ),
    )

    assert result.content == "来自自定义模型"
    assert captured["api_key"] == "user-key"
    assert captured["base_url"] == "https://example.test/v1"
    assert captured["model"] == "custom-model"


def test_llm_client_stream_generate_uses_native_stream(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeFunctionDelta:
        def __init__(self, *, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class FakeToolCallDelta:
        def __init__(self, *, index: int, call_id: str | None = None, function: FakeFunctionDelta | None = None) -> None:
            self.index = index
            self.id = call_id
            self.function = function

    class FakeDelta:
        def __init__(self, *, content: str | None = None, tool_calls: list[FakeToolCallDelta] | None = None) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class FakeChoice:
        def __init__(self, delta: FakeDelta) -> None:
            self.delta = delta

    class FakeChunk:
        def __init__(self, delta: FakeDelta) -> None:
            self.choices = [FakeChoice(delta)]

    class FakeStream:
        def __init__(self, chunks) -> None:  # noqa: ANN001
            self._chunks = iter(chunks)
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def close(self) -> None:
            self.closed = True

    class FakeCompletions:
        async def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            stream = FakeStream(
                [
                    FakeChunk(FakeDelta(content="你")),
                    FakeChunk(FakeDelta(content="好")),
                    FakeChunk(
                        FakeDelta(
                            tool_calls=[
                                FakeToolCallDelta(
                                    index=0,
                                    call_id="call-1",
                                    function=FakeFunctionDelta(name="search_text", arguments='{"query":'),
                                )
                            ]
                        )
                    ),
                    FakeChunk(
                        FakeDelta(
                            tool_calls=[
                                FakeToolCallDelta(
                                    index=0,
                                    function=FakeFunctionDelta(arguments='"TODO","path":"."}'),
                                )
                            ]
                        )
                    ),
                ]
            )
            captured["stream_instance"] = stream
            return stream

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.chat = FakeChat()

        async def close(self) -> None:
            captured["client_closed"] = True

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeAsyncOpenAI)
    client = llm_module.LLMClient(api_key="", base_url="https://api.openai.com/v1")

    async def collect():
        return [
            event
            async for event in client.stream_generate(
                messages=[{"role": "user", "content": "搜索 TODO"}],
                tools=[],
                settings=llm_module.AgentSettings(
                    api_key="stream-key",
                    api_base_url="https://example.test/v1",
                    model="stream-model",
                ),
            )
        ]

    events = asyncio.run(collect())

    assert captured["stream"] is True
    assert captured["api_key"] == "stream-key"
    assert captured["base_url"] == "https://example.test/v1"
    assert captured["model"] == "stream-model"
    assert "".join(event.content_delta or "" for event in events) == "你好"
    assert events[-1].result is not None
    assert events[-1].result.tool_calls[0].id == "call-1"
    assert events[-1].result.tool_calls[0].name == "search_text"
    assert events[-1].result.tool_calls[0].arguments == {"query": "TODO", "path": "."}
    assert captured["stream_instance"].closed is True
    assert captured["client_closed"] is True
