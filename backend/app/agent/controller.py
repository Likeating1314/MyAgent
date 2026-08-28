from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from uuid import uuid4

from app.agent.llm_client import LLMClient, LLMResult, LLMStreamEvent, LLMToolCall
from app.agent.context_builder import ContextBudgetError, ContextBuilder
from app.agent.tool_executor import ToolContext, ToolExecutor
from app.agent.tool_registry import ToolRegistry, build_default_registry
from app.config import AppSettings
from app.models.schemas import (
    AgentSettings,
    ApprovalResumeRequest,
    ChatRequest,
    ChatResponse,
    MessageSchema,
    ToolCallRecord,
)
from app.services.approval_store import ApprovalResumeError, ApprovalStore
from app.services.rag_store import RagStore
from app.services.session_store import (
    InMemorySessionStore,
    SessionEventInput,
    SessionLeaseLostError,
    SessionStore,
    SQLiteSessionStore,
)
from app.services.stream_lease import StreamRunLeaseGuard
from app.security import current_user_id, reset_current_user, set_current_user

logger = logging.getLogger(__name__)
CancellationCheck = Callable[[], Awaitable[bool]]


class StreamCancelled(Exception):
    pass


class UserMessageTooLargeError(ValueError):
    pass


class AgentController:
    def __init__(
        self,
        *,
        app_settings: AppSettings,
        session_store: SessionStore | None = None,
        approval_store: ApprovalStore | None = None,
        rag_store: RagStore | None = None,
        registry: ToolRegistry | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.app_settings = app_settings
        self.session_store = session_store or InMemorySessionStore()
        self.approval_store = approval_store
        self.rag_store = rag_store
        self.registry = registry or build_default_registry()
        self.context_builder = ContextBuilder(app_settings.max_context_chars)
        self.llm_client = llm_client or LLMClient(
            api_key=app_settings.openai_api_key,
            base_url=app_settings.openai_base_url,
            max_content_chars=app_settings.max_message_chars,
        )

    def _merge_settings(self, request_settings: AgentSettings | None) -> AgentSettings:
        base = AgentSettings(
            api_provider="openai",
            api_key=self.app_settings.openai_api_key,
            model=self.app_settings.openai_model,
            api_base_url=self.app_settings.openai_base_url,
            allow_command_execution=self.app_settings.allow_command_execution,
            max_agent_steps=self.app_settings.max_agent_steps,
        )
        if request_settings is None:
            return base
        return AgentSettings(
            api_provider=request_settings.api_provider or base.api_provider,
            api_key=request_settings.api_key or base.api_key,
            model=request_settings.model or base.model,
            api_base_url=request_settings.api_base_url or base.api_base_url,
            allow_command_execution=(
                base.allow_command_execution and request_settings.allow_command_execution
            ),
            max_agent_steps=request_settings.max_agent_steps or base.max_agent_steps,
            use_streaming=request_settings.use_streaming,
        )

    def validate_request(self, request: ChatRequest) -> None:
        if len(request.message) > self.app_settings.max_user_message_chars:
            raise UserMessageTooLargeError("用户消息超过服务端长度上限")
        try:
            self.context_builder.build(
                [{"role": "user", "content": request.message}], current_user_index=0
            )
        except ContextBudgetError as exc:
            raise UserMessageTooLargeError("用户消息与 system prompt 超过上下文预算") from exc

    def acquire_session_run(self, session_id: str) -> str:
        return self.session_store.acquire_run(
            session_id, lease_seconds=self.app_settings.session_run_lease_seconds
        )

    def handle_chat(self, request: ChatRequest, *, run_id: str | None = None) -> ChatResponse:
        self.validate_request(request)
        run_id = run_id or self.acquire_session_run(request.session_id)
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        owner_user_id = current_user_id()
        heartbeat = threading.Thread(
            target=self._sync_lease_heartbeat_for_user,
            args=(owner_user_id, request.session_id, run_id, heartbeat_stop, lease_lost),
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._handle_chat_with_lease(request, run_id, lease_lost)
        finally:
            heartbeat_stop.set()
            heartbeat.join()
            self.session_store.release_run(request.session_id, run_id)

    def _handle_chat_with_lease(
        self, request: ChatRequest, run_id: str, lease_lost: threading.Event
    ) -> ChatResponse:
        settings = self._merge_settings(request.settings)
        self._raise_if_sync_lease_lost(lease_lost)
        self.session_store.get_or_create(request.session_id)
        user_message = {"role": "user", "content": request.message}
        self.session_store.append_event(
            request.session_id, "message", user_message, run_id=run_id
        )
        session = self.session_store.get(request.session_id)
        if session is None:
            raise RuntimeError("会话读取失败")
        conversation = list(session.messages)
        current_user_index = len(conversation) - 1

        tool_context = ToolContext(
            workspace_dir=self.app_settings.resolved_user_workspace_dir(current_user_id()),
            user_id=current_user_id(),
            session_id=request.session_id,
            allow_command_execution=settings.allow_command_execution,
            approval_store=self.approval_store,
            rag_store=self.rag_store,
            max_result_chars=self.app_settings.max_tool_result_chars,
            max_command_output_chars=self.app_settings.max_command_output_chars,
        )
        executor = ToolExecutor(self.registry, tool_context)
        tool_records: list[ToolCallRecord] = []

        for _ in range(settings.max_agent_steps):
            self._raise_if_sync_lease_lost(lease_lost)
            model_context = self.context_builder.build(
                conversation, current_user_index=current_user_index
            )
            try:
                result = self.llm_client.generate(
                    messages=model_context,
                    tools=self.registry.list_tool_schemas(),
                    settings=settings,
                )
            except Exception as exc:
                if lease_lost.is_set():
                    raise SessionLeaseLostError("会话 run 租约已失效") from exc
                raise
            self._raise_if_sync_lease_lost(lease_lost)
            result.content = self._bounded_assistant_text(result.content)
            if result.has_tool_calls:
                for tool_index, tool_call in enumerate(result.tool_calls):
                    self._renew_sync_lease_or_raise(request.session_id, run_id, lease_lost)
                    assistant_stub = self._tool_assistant_message(
                        tool_call,
                        content=result.content if tool_index == 0 else None,
                    )
                    record = executor.execute_model_call(tool_call.name, tool_call.arguments)
                    self._raise_if_sync_lease_lost(lease_lost)
                    tool_records.append(record)
                    tool_message = executor.result_to_tool_message(tool_call.id, record)
                    self.session_store.append_batch(
                        request.session_id,
                        self._tool_event_batch(assistant_stub, record, tool_message),
                        run_id=run_id,
                    )
                    conversation.extend((assistant_stub, tool_message))
                continue

            self._raise_if_sync_lease_lost(lease_lost)
            answer = result.content or "模型没有返回最终答案。"
            assistant_message = {"role": "assistant", "content": answer}
            self.session_store.append_event(
                request.session_id, "message", assistant_message, run_id=run_id
            )
            conversation.append(assistant_message)
            return ChatResponse(
                session_id=request.session_id,
                answer=answer,
                tool_calls=tool_records,
                messages=[MessageSchema.model_validate(item) for item in conversation],
            )

        self._raise_if_sync_lease_lost(lease_lost)
        answer = "已达到最大 Agent 步数，停止继续循环。"
        assistant_message = {"role": "assistant", "content": answer}
        self.session_store.append_event(
            request.session_id, "message", assistant_message, run_id=run_id
        )
        conversation.append(assistant_message)
        return ChatResponse(
            session_id=request.session_id,
            answer=answer,
            tool_calls=tool_records,
            messages=[MessageSchema.model_validate(item) for item in conversation],
        )

    async def handle_chat_stream(
        self,
        request: ChatRequest,
        is_cancelled: CancellationCheck | None = None,
        run_id: str | None = None,
        lease_guard: StreamRunLeaseGuard | None = None,
    ) -> AsyncIterator[str]:
        stage = "session"
        terminal_sent = False
        lease_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = None
        try:
            self.validate_request(request)
            if run_id is None:
                run_id = await asyncio.to_thread(self.acquire_session_run, request.session_id)
            heartbeat = asyncio.create_task(
                self._async_lease_heartbeat(
                    request.session_id, run_id, lease_stop, lease_lost
                )
            )
            settings = self._merge_settings(request.settings)
            await self._raise_if_stopped(is_cancelled, lease_lost)
            await asyncio.to_thread(self.session_store.get_or_create, request.session_id)
            user_message = {"role": "user", "content": request.message}
            await asyncio.to_thread(
                self.session_store.append_event,
                request.session_id,
                "message",
                user_message,
                run_id=run_id,
            )
            session = await asyncio.to_thread(self.session_store.get, request.session_id)
            if session is None:
                raise RuntimeError("会话读取失败")
            conversation = list(session.messages)
            current_user_index = len(conversation) - 1
            tool_context = ToolContext(
                workspace_dir=self.app_settings.resolved_user_workspace_dir(current_user_id()),
                user_id=current_user_id(),
                session_id=request.session_id,
                allow_command_execution=settings.allow_command_execution,
                approval_store=self.approval_store,
                rag_store=self.rag_store,
                max_result_chars=self.app_settings.max_tool_result_chars,
                max_command_output_chars=self.app_settings.max_command_output_chars,
            )
            executor = ToolExecutor(self.registry, tool_context)
            tool_records: list[ToolCallRecord] = []

            for _ in range(settings.max_agent_steps):
                await self._raise_if_stopped(is_cancelled, lease_lost)
                stage = "model"
                result: LLMResult | None = None
                model_context = self.context_builder.build(
                    conversation, current_user_index=current_user_index
                )
                stream = self._stream_llm(model_context, settings)
                async for event in self._stream_until_cancelled(
                    stream, is_cancelled, lease_lost
                ):
                    if event.content_delta:
                        yield self._sse_event("delta", {"content": event.content_delta})
                    if event.result is not None:
                        result = event.result

                if result is None:
                    raise RuntimeError("模型流未返回结果")
                result.content = self._bounded_assistant_text(result.content)
                await self._raise_if_stopped(is_cancelled, lease_lost)

                if result.has_tool_calls:
                    for tool_index, tool_call in enumerate(result.tool_calls):
                        await self._raise_if_stopped(is_cancelled, lease_lost)
                        await self._renew_async_lease_or_raise(
                            request.session_id, run_id, lease_lost
                        )
                        assistant_stub = self._tool_assistant_message(
                            tool_call,
                            content=result.content if tool_index == 0 else None,
                        )
                        stage = "tool"
                        tool_task = asyncio.create_task(
                            asyncio.to_thread(
                                executor.execute_model_call, tool_call.name, tool_call.arguments
                            )
                        )
                        runtime_cancelled = False
                        try:
                            record = await asyncio.shield(tool_task)
                        except asyncio.CancelledError:
                            runtime_cancelled = True
                            record = await tool_task
                        self._raise_if_async_lease_lost(lease_lost)
                        tool_message = executor.result_to_tool_message(tool_call.id, record)
                        stage = "session"
                        await self._append_tool_batch_after_execution(
                            request.session_id, run_id, assistant_stub, record, tool_message
                        )
                        tool_records.append(record)
                        conversation.extend((assistant_stub, tool_message))
                        if runtime_cancelled:
                            return
                        await self._raise_if_stopped(is_cancelled, lease_lost)
                        yield self._sse_event("tool_call", record.model_dump(mode="json"))
                    continue

                answer = result.content or "模型没有返回最终答案。"
                assistant_message = {"role": "assistant", "content": answer}
                stage = "session"
                await asyncio.to_thread(
                    self.session_store.append_event,
                    request.session_id,
                    "message",
                    assistant_message,
                    run_id=run_id,
                )
                conversation.append(assistant_message)
                terminal_sent = True
                yield self._sse_event(
                    "done",
                    ChatResponse(
                        session_id=request.session_id,
                        answer=answer,
                        tool_calls=tool_records,
                        messages=[MessageSchema.model_validate(item) for item in conversation],
                    ).model_dump(mode="json"),
                )
                return

            await self._raise_if_stopped(is_cancelled, lease_lost)
            answer = "已达到最大 Agent 步数，停止继续循环。"
            assistant_message = {"role": "assistant", "content": answer}
            stage = "session"
            await asyncio.to_thread(
                self.session_store.append_event,
                request.session_id,
                "message",
                assistant_message,
                run_id=run_id,
            )
            conversation.append(assistant_message)
            yield self._sse_event("delta", {"content": answer})
            terminal_sent = True
            yield self._sse_event(
                "done",
                ChatResponse(
                    session_id=request.session_id,
                    answer=answer,
                    tool_calls=tool_records,
                    messages=[MessageSchema.model_validate(item) for item in conversation],
                ).model_dump(mode="json"),
            )
        except SessionLeaseLostError:
            if not terminal_sent:
                terminal_sent = True
                yield self._sse_event(
                    "error",
                    {
                        "code": "session_lease_lost",
                        "message": "会话执行权已失效，请重试。",
                        "session_id": request.session_id,
                    },
                )
        except StreamCancelled:
            if not terminal_sent:
                terminal_sent = True
                yield self._sse_event(
                    "cancelled",
                    {"code": "cancelled", "message": "任务已取消。", "session_id": request.session_id},
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            if not terminal_sent:
                terminal_sent = True
                if lease_lost.is_set():
                    code, message = "session_lease_lost", "会话执行权已失效，请重试。"
                else:
                    logger.error(
                        "stream failed stage=%s exception_type=%s",
                        stage,
                        type(exc).__name__,
                    )
                    code, message = self._safe_stream_error(stage)
                yield self._sse_event(
                    "error",
                    {"code": code, "message": message, "session_id": request.session_id},
                )
        finally:
            lease_stop.set()
            if heartbeat is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            if lease_guard is not None:
                await lease_guard.close()
            elif run_id is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.session_store.release_run, request.session_id, run_id
                    )

    async def handle_approval_resume_stream(
        self,
        approval_id: str,
        request: ApprovalResumeRequest,
        *,
        is_cancelled: CancellationCheck | None = None,
        run_id: str,
        lease_guard: StreamRunLeaseGuard | None = None,
    ) -> AsyncIterator[str]:
        stage = "session"
        terminal_sent = False
        lease_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = None
        session_id = ""
        try:
            if self.approval_store is None:
                raise ApprovalResumeError("approval_unavailable", "审批服务不可用。")
            approval_task = asyncio.create_task(
                asyncio.to_thread(self.approval_store.require_resumable, approval_id)
            )
            try:
                approval = await asyncio.shield(approval_task)
            except asyncio.CancelledError:
                approval = await approval_task
                session_id = approval.session_id
                await self._set_approval_resume_outcome(
                    approval_id, session_id, run_id, "cancelled"
                )
                raise
            session_id = approval.session_id
            cleared = await self._set_approval_resume_outcome(
                approval_id, session_id, run_id, None, require_success=True
            )
            heartbeat = asyncio.create_task(
                self._async_lease_heartbeat(session_id, run_id, lease_stop, lease_lost)
            )
            settings = self._merge_settings(request.settings)
            await self._raise_if_stopped(is_cancelled, lease_lost)

            session = await asyncio.to_thread(self.session_store.get, session_id)
            if session is None:
                raise ApprovalResumeError(
                    "approval_session_not_found", "审批对应的会话不存在。", status_code=404
                )
            current_user_index = next(
                (
                    index
                    for index in range(len(session.messages) - 1, -1, -1)
                    if session.messages[index].get("role") == "user"
                ),
                None,
            )
            if current_user_index is None:
                raise ApprovalResumeError(
                    "approval_history_invalid", "审批对应会话缺少原始用户消息，不能安全续跑。"
                )
            conversation = list(session.messages)
            tool_context = ToolContext(
                workspace_dir=self.app_settings.resolved_user_workspace_dir(current_user_id()),
                user_id=current_user_id(),
                session_id=session_id,
                allow_command_execution=settings.allow_command_execution,
                approval_store=self.approval_store,
                rag_store=self.rag_store,
                max_result_chars=self.app_settings.max_tool_result_chars,
                max_command_output_chars=self.app_settings.max_command_output_chars,
            )
            executor = ToolExecutor(self.registry, tool_context)
            tool_records: list[ToolCallRecord] = list(session.tool_calls)

            await self._renew_async_lease_or_raise(session_id, run_id, lease_lost)
            tool_call = LLMToolCall(
                id=f"resume_{uuid4().hex}",
                name=approval.tool_name,
                arguments={**approval.arguments, "approval_id": approval.id},
            )
            assistant_stub = self._tool_assistant_message(
                tool_call, content=f"已批准，继续调用工具：{approval.tool_name}"
            )
            stage = "tool"
            tool_task = asyncio.create_task(
                asyncio.to_thread(
                    executor.execute_approved,
                    tool_call.name,
                    approval.arguments,
                    approval_id=approval.id,
                )
            )
            runtime_cancelled = False
            try:
                record = await asyncio.shield(tool_task)
            except asyncio.CancelledError:
                runtime_cancelled = True
                record = await tool_task
            self._raise_if_async_lease_lost(lease_lost)
            tool_message = executor.result_to_tool_message(tool_call.id, record)
            stage = "session"
            await self._append_tool_batch_after_execution(
                session_id, run_id, assistant_stub, record, tool_message
            )
            tool_records.append(record)
            conversation.extend((assistant_stub, tool_message))
            if runtime_cancelled:
                await self._set_approval_resume_outcome(
                    approval_id, session_id, run_id, "cancelled"
                )
                raise asyncio.CancelledError
            await self._raise_if_stopped(is_cancelled, lease_lost)
            yield self._sse_event("tool_call", record.model_dump(mode="json"))

            for _ in range(settings.max_agent_steps):
                await self._raise_if_stopped(is_cancelled, lease_lost)
                stage = "model"
                result: LLMResult | None = None
                model_context = self.context_builder.build(
                    conversation, current_user_index=current_user_index
                )
                async for event in self._stream_until_cancelled(
                    self._stream_llm(model_context, settings), is_cancelled, lease_lost
                ):
                    if event.content_delta:
                        yield self._sse_event("delta", {"content": event.content_delta})
                    if event.result is not None:
                        result = event.result
                if result is None:
                    raise RuntimeError("模型流未返回结果")
                result.content = self._bounded_assistant_text(result.content)
                await self._raise_if_stopped(is_cancelled, lease_lost)

                if result.has_tool_calls:
                    for tool_index, next_call in enumerate(result.tool_calls):
                        await self._raise_if_stopped(is_cancelled, lease_lost)
                        await self._renew_async_lease_or_raise(
                            session_id, run_id, lease_lost
                        )
                        next_assistant = self._tool_assistant_message(
                            next_call,
                            content=result.content if tool_index == 0 else None,
                        )
                        stage = "tool"
                        next_task = asyncio.create_task(
                            asyncio.to_thread(
                                executor.execute_model_call, next_call.name, next_call.arguments
                            )
                        )
                        next_runtime_cancelled = False
                        try:
                            next_record = await asyncio.shield(next_task)
                        except asyncio.CancelledError:
                            next_runtime_cancelled = True
                            next_record = await next_task
                        self._raise_if_async_lease_lost(lease_lost)
                        next_message = executor.result_to_tool_message(
                            next_call.id, next_record
                        )
                        stage = "session"
                        await self._append_tool_batch_after_execution(
                            session_id,
                            run_id,
                            next_assistant,
                            next_record,
                            next_message,
                        )
                        tool_records.append(next_record)
                        conversation.extend((next_assistant, next_message))
                        if next_runtime_cancelled:
                            await self._set_approval_resume_outcome(
                                approval_id, session_id, run_id, "cancelled"
                            )
                            raise asyncio.CancelledError
                        await self._raise_if_stopped(is_cancelled, lease_lost)
                        yield self._sse_event(
                            "tool_call", next_record.model_dump(mode="json")
                        )
                    continue

                answer = result.content or "模型没有返回最终答案。"
                assistant_message = {"role": "assistant", "content": answer}
                stage = "session"
                await asyncio.to_thread(
                    self.session_store.append_event,
                    session_id,
                    "message",
                    assistant_message,
                    run_id=run_id,
                )
                conversation.append(assistant_message)
                terminal_sent = True
                yield self._sse_event(
                    "done",
                    ChatResponse(
                        session_id=session_id,
                        answer=answer,
                        tool_calls=tool_records,
                        messages=[MessageSchema.model_validate(item) for item in conversation],
                    ).model_dump(mode="json"),
                )
                return

            await self._raise_if_stopped(is_cancelled, lease_lost)
            answer = "已达到最大 Agent 步数，停止继续循环。"
            assistant_message = {"role": "assistant", "content": answer}
            stage = "session"
            await asyncio.to_thread(
                self.session_store.append_event,
                session_id,
                "message",
                assistant_message,
                run_id=run_id,
            )
            conversation.append(assistant_message)
            yield self._sse_event("delta", {"content": answer})
            terminal_sent = True
            yield self._sse_event(
                "done",
                ChatResponse(
                    session_id=session_id,
                    answer=answer,
                    tool_calls=tool_records,
                    messages=[MessageSchema.model_validate(item) for item in conversation],
                ).model_dump(mode="json"),
            )
        except ApprovalResumeError as exc:
            if not terminal_sent:
                terminal_sent = True
                yield self._sse_event(
                    "error",
                    {
                        "code": exc.code,
                        "message": str(exc),
                        "session_id": session_id or exc.session_id,
                    },
                )
        except SessionLeaseLostError:
            if not terminal_sent:
                terminal_sent = True
                yield self._sse_event(
                    "error",
                    {
                        "code": "session_lease_lost",
                        "message": "会话执行权已失效，请重试。",
                        "session_id": session_id or None,
                    },
                )
        except StreamCancelled:
            outcome_saved = False
            cancel_lease_lost = False
            cancel_persistence_failed = False
            if self.approval_store is not None and session_id:
                try:
                    outcome_saved = await self._set_approval_resume_outcome(
                        approval_id,
                        session_id,
                        run_id,
                        "cancelled",
                        require_success=True,
                    )
                except SessionLeaseLostError:
                    cancel_lease_lost = True
                except Exception as exc:  # noqa: BLE001
                    cancel_persistence_failed = True
                    logger.warning(
                        "approval cancellation persistence failed session_id=%s exception_type=%s",
                        session_id,
                        type(exc).__name__,
                    )
            if not terminal_sent:
                terminal_sent = True
                if outcome_saved:
                    yield self._sse_event(
                        "cancelled",
                        {"code": "cancelled", "message": "任务已取消。", "session_id": session_id},
                    )
                elif cancel_lease_lost or lease_lost.is_set():
                    yield self._sse_event(
                        "error",
                        {
                            "code": "session_lease_lost",
                            "message": "会话执行权已失效，请刷新状态。",
                            "session_id": session_id,
                        },
                    )
                elif cancel_persistence_failed:
                    yield self._sse_event(
                        "error",
                        {
                            "code": "session_error",
                            "message": "取消状态保存失败，请刷新会话状态。",
                            "session_id": session_id,
                        },
                    )
        except asyncio.CancelledError:
            if self.approval_store is not None and session_id:
                with contextlib.suppress(Exception):
                    await self._set_approval_resume_outcome(
                        approval_id, session_id, run_id, "cancelled"
                    )
            raise
        except GeneratorExit:
            if self.approval_store is not None and session_id:
                with contextlib.suppress(Exception):
                    await self._set_approval_resume_outcome(
                        approval_id, session_id, run_id, "cancelled"
                    )
            raise
        except Exception as exc:  # noqa: BLE001
            if not terminal_sent:
                terminal_sent = True
                if lease_lost.is_set():
                    code, message = "session_lease_lost", "会话执行权已失效，请重试。"
                else:
                    logger.error(
                        "approval resume stream failed stage=%s exception_type=%s",
                        stage,
                        type(exc).__name__,
                    )
                    code, message = self._safe_stream_error(stage)
                yield self._sse_event(
                    "error", {"code": code, "message": message, "session_id": session_id}
                )
        finally:
            lease_stop.set()
            if heartbeat is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            if lease_guard is not None:
                await lease_guard.close()
            elif session_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.session_store.release_run, session_id, run_id
                    )

    async def _stream_llm(
        self, conversation: list[dict[str, Any]], settings: AgentSettings
    ) -> AsyncIterator[LLMStreamEvent]:
        stream_generate = getattr(self.llm_client, "stream_generate", None)
        if callable(stream_generate):
            stream = stream_generate(
                messages=conversation,
                tools=self.registry.list_tool_schemas(),
                settings=settings,
            )
            try:
                async for event in stream:
                    yield event
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await close()
            return

        result = await asyncio.to_thread(
            self.llm_client.generate,
            messages=conversation,
            tools=self.registry.list_tool_schemas(),
            settings=settings,
        )
        if result.content:
            for chunk in self._chunk_text(result.content):
                yield LLMStreamEvent(content_delta=chunk)
                await asyncio.sleep(0)
        yield LLMStreamEvent(result=result)

    async def _stream_until_cancelled(
        self,
        stream: AsyncIterator[LLMStreamEvent],
        is_cancelled: CancellationCheck | None,
        lease_lost: asyncio.Event,
    ) -> AsyncIterator[LLMStreamEvent]:
        iterator = aiter(stream)
        next_event: asyncio.Task[LLMStreamEvent] | None = None
        stop_waiter: asyncio.Task[str] | None = None
        try:
            while True:
                await self._raise_if_stopped(is_cancelled, lease_lost)
                next_event = asyncio.create_task(anext(iterator))
                stop_waiter = asyncio.create_task(
                    self._wait_for_stream_stop(is_cancelled, lease_lost)
                )
                try:
                    done, _ = await asyncio.wait(
                        {next_event, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_waiter in done:
                        reason = stop_waiter.result()
                        next_event.cancel()
                        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                            await next_event
                        next_event = None
                        if reason == "lease_lost":
                            raise SessionLeaseLostError("会话 run 租约已失效")
                        raise StreamCancelled
                    if lease_lost.is_set():
                        next_event.cancel()
                        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                            await next_event
                        next_event = None
                        raise SessionLeaseLostError("会话 run 租约已失效")
                    stop_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_waiter
                    stop_waiter = None
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                finally:
                    if stop_waiter is not None:
                        stop_waiter.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_waiter
                        stop_waiter = None
                    if next_event is not None and next_event.done():
                        next_event = None
                yield event
        finally:
            for task in (next_event, stop_waiter):
                if task is not None and not task.done():
                    task.cancel()
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await task
            close = getattr(iterator, "aclose", None)
            if callable(close):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await close()

    async def _wait_for_stream_stop(
        self,
        is_cancelled: CancellationCheck | None,
        lease_lost: asyncio.Event,
    ) -> str:
        while True:
            if lease_lost.is_set():
                return "lease_lost"
            if is_cancelled is not None and await self._cancel_requested(is_cancelled):
                return "cancelled"
            try:
                await asyncio.wait_for(lease_lost.wait(), timeout=0.05)
                return "lease_lost"
            except TimeoutError:
                pass

    async def _raise_if_stopped(
        self, is_cancelled: CancellationCheck | None, lease_lost: asyncio.Event
    ) -> None:
        self._raise_if_async_lease_lost(lease_lost)
        if is_cancelled is not None and await self._cancel_requested(is_cancelled):
            raise StreamCancelled

    @staticmethod
    async def _cancel_requested(is_cancelled: CancellationCheck) -> bool:
        try:
            return bool(await is_cancelled())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return True

    async def _append_tool_batch_after_execution(
        self,
        session_id: str,
        run_id: str,
        assistant_message: dict[str, Any],
        record: ToolCallRecord,
        tool_message: dict[str, Any],
    ) -> None:
        save_task = asyncio.create_task(
            asyncio.to_thread(
                self.session_store.append_batch,
                session_id,
                self._tool_event_batch(assistant_message, record, tool_message),
                run_id=run_id,
            )
        )
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            await save_task
            raise

    async def _set_approval_resume_outcome(
        self,
        approval_id: str,
        session_id: str,
        run_id: str,
        outcome: str | None,
        require_success: bool = False,
    ) -> bool:
        if self.approval_store is None:
            return False
        if isinstance(self.session_store, SQLiteSessionStore):
            if self.session_store.database_path.resolve() != self.approval_store.database_path.resolve():
                if require_success:
                    raise SessionLeaseLostError("审批存储与会话租约不在同一数据库")
                return False
            update_task = asyncio.create_task(
                asyncio.to_thread(
                    self.approval_store.set_resume_outcome_fenced,
                    approval_id,
                    outcome,
                    session_id=session_id,
                    run_id=run_id,
                )
            )
            try:
                result = await asyncio.shield(update_task)
            except asyncio.CancelledError:
                await update_task
                raise
            if require_success and not result:
                raise SessionLeaseLostError("会话 run 租约已失效")
            return result

        try:
            owns_lease = await asyncio.to_thread(
                self.session_store.renew_run,
                session_id,
                run_id,
                lease_seconds=self.app_settings.session_run_lease_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            if require_success:
                raise SessionLeaseLostError("无法确认会话 run 租约") from exc
            return False
        if not owns_lease:
            if require_success:
                raise SessionLeaseLostError("会话 run 租约已失效")
            return False
        if outcome == "cancelled":
            session = await asyncio.to_thread(self.session_store.get, session_id)
            if session is not None and any(
                record.arguments.get("approval_id") == approval_id
                for record in session.tool_calls
            ):
                return True
        await asyncio.to_thread(
            self.approval_store.set_resume_outcome_unfenced_for_memory,
            approval_id,
            outcome,
            session_id=session_id,
        )
        return True

    def _sync_lease_heartbeat(
        self,
        session_id: str,
        run_id: str,
        stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        interval = max(1.0, self.app_settings.session_run_lease_seconds / 3)
        while not stop.wait(interval):
            try:
                if not self.session_store.renew_run(
                    session_id,
                    run_id,
                    lease_seconds=self.app_settings.session_run_lease_seconds,
                ):
                    lease_lost.set()
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "session lease renewal failed session_id=%s exception_type=%s",
                    session_id,
                    type(exc).__name__,
                )
                lease_lost.set()
                return

    async def _async_lease_heartbeat(
        self,
        session_id: str,
        run_id: str,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(1.0, self.app_settings.session_run_lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    renewed = await asyncio.to_thread(
                        self.session_store.renew_run,
                        session_id,
                        run_id,
                        lease_seconds=self.app_settings.session_run_lease_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "session lease renewal failed session_id=%s exception_type=%s",
                        session_id,
                        type(exc).__name__,
                    )
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

    def _sync_lease_heartbeat_for_user(self, user_id, *args) -> None:  # noqa: ANN001
        token = set_current_user(user_id)
        try:
            self._sync_lease_heartbeat(*args)
        finally:
            reset_current_user(token)

    @staticmethod
    def _raise_if_sync_lease_lost(lease_lost: threading.Event) -> None:
        if lease_lost.is_set():
            raise SessionLeaseLostError("会话 run 租约已失效")

    @staticmethod
    def _raise_if_async_lease_lost(lease_lost: asyncio.Event) -> None:
        if lease_lost.is_set():
            raise SessionLeaseLostError("会话 run 租约已失效")

    def _renew_sync_lease_or_raise(
        self, session_id: str, run_id: str, lease_lost: threading.Event
    ) -> None:
        self._raise_if_sync_lease_lost(lease_lost)
        try:
            renewed = self.session_store.renew_run(
                session_id,
                run_id,
                lease_seconds=self.app_settings.session_run_lease_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            lease_lost.set()
            raise SessionLeaseLostError("无法确认会话 run 租约") from exc
        if not renewed:
            lease_lost.set()
            raise SessionLeaseLostError("会话 run 租约已失效")

    async def _renew_async_lease_or_raise(
        self, session_id: str, run_id: str, lease_lost: asyncio.Event
    ) -> None:
        self._raise_if_async_lease_lost(lease_lost)
        try:
            renewed = await asyncio.to_thread(
                self.session_store.renew_run,
                session_id,
                run_id,
                lease_seconds=self.app_settings.session_run_lease_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            lease_lost.set()
            raise SessionLeaseLostError("无法确认会话 run 租约") from exc
        if not renewed:
            lease_lost.set()
            raise SessionLeaseLostError("会话 run 租约已失效")

    @staticmethod
    def _tool_assistant_message(tool_call, *, content: str | None) -> dict[str, Any]:  # noqa: ANN001
        return {
            "role": "assistant",
            "content": content or f"准备调用工具：{tool_call.name}",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                    },
                }
            ],
        }

    @staticmethod
    def _tool_event_batch(
        assistant_message: dict[str, Any],
        record: ToolCallRecord,
        tool_message: dict[str, Any],
    ) -> list[SessionEventInput]:
        return [
            ("message", assistant_message),
            ("tool_call", record),
            ("message", tool_message),
        ]

    def _bounded_assistant_text(self, content: str | None) -> str | None:
        if content is None or len(content) <= self.app_settings.max_message_chars:
            return content
        marker = "\n[模型输出已截断]"
        return content[: max(0, self.app_settings.max_message_chars - len(marker))] + marker

    @staticmethod
    def _safe_stream_error(stage: str) -> tuple[str, str]:
        if stage == "model":
            return "model_error", "模型服务暂时不可用，请稍后重试。"
        if stage == "tool":
            return "tool_error", "工具执行失败，请检查工具记录后重试。"
        return "session_error", "会话保存失败，请重试。"

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 18) -> Iterator[str]:
        for index in range(0, len(text), chunk_size):
            yield text[index : index + chunk_size]

    @staticmethod
    def _sse_event(event: str, payload: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
