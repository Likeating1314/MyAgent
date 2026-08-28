from __future__ import annotations

from dataclasses import dataclass

from app.agent.controller import AgentController
from app.agent.llm_client import LLMResult, LLMToolCall
from app.config import AppSettings
from app.models.schemas import ChatRequest


@dataclass
class SequenceLLMClient:
    responses: list[LLMResult]

    def generate(self, **kwargs):  # noqa: ANN003
        return self.responses.pop(0)


def test_agent_loop_executes_tool(tmp_path):
    settings = AppSettings(
        OPENAI_API_KEY="",
        OPENAI_BASE_URL="https://api.openai.com/v1",
        OPENAI_MODEL="gpt-4.1-mini",
        WORKSPACE_DIR=str(tmp_path),
        ALLOW_COMMAND_EXECUTION=False,
        MAX_AGENT_STEPS=4,
    )
    (tmp_path / "todo.txt").write_text("TODO: one", encoding="utf-8")
    llm = SequenceLLMClient(
        responses=[
            LLMResult(content=None, tool_calls=[LLMToolCall(id="1", name="search_text", arguments={"query": "TODO", "path": "."})]),
            LLMResult(content="完成了", tool_calls=[]),
        ]
    )
    controller = AgentController(app_settings=settings, llm_client=llm)
    response = controller.handle_chat(ChatRequest(session_id="s1", message="搜索 TODO"))
    assert response.answer == "完成了"
    assert response.tool_calls[0].name == "search_text"
