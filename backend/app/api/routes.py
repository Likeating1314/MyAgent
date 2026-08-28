from __future__ import annotations

import asyncio
import unicodedata
import weakref
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app import SERVICE_NAME, SERVICE_VERSION
from app.agent.controller import AgentController, UserMessageTooLargeError
from app.models.schemas import (
    ApprovalRequest,
    ApprovalResumeRequest,
    ChatRequest,
    ChatResponse,
    MessageSchema,
    RuntimeInfo,
    SessionInfo,
    SessionSummary,
    SessionRenameRequest,
    CollaborationCreateRequest,
    CollaborationInfo,
    CollaborationRunRequest,
    CollaborationSummary,
)
from app.security import TEST_OWNER_USER_ID, require_local_agent_token, require_user_principal
from app.services.approval_store import ApprovalMutationError, ApprovalResumeError
from app.services.session_store import (
    SessionArchivedError,
    SessionBusyError,
    SessionLeaseLostError,
    SessionNotFoundError,
    SessionOpenApprovalError,
    summarize_session,
)
from app.services.stream_lease import LeaseBodyIterator, StreamRunLeaseGuard
from app.services.collaboration_store import (
    CollaborationBusyError,
    CollaborationNotFoundError,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_user_principal)])
runtime_router = APIRouter(prefix="/api", dependencies=[Depends(require_local_agent_token)])


class LeaseStreamingResponse(StreamingResponse):
    def __init__(self, content, *, lease_guard: StreamRunLeaseGuard, **kwargs) -> None:  # noqa: ANN001, ANN003
        super().__init__(LeaseBodyIterator(content, lease_guard), **kwargs)
        if not lease_guard.claim_response():
            raise RuntimeError("stream lease response handoff failed")
        self.lease_guard = lease_guard
        self._lease_finalizer = weakref.finalize(self, lease_guard.close_sync)

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.lease_guard.close()


def _session_busy_http_error(exc: SessionBusyError) -> HTTPException:
    del exc
    return HTTPException(
        status_code=409,
        detail={
            "code": "session_busy",
            "message": "上一任务仍在完成已启动的工具，请稍后重试。",
        },
    )


async def _acquire_stream_run(
    controller: AgentController, session_id: str
) -> tuple[str, StreamRunLeaseGuard]:
    acquire_task = asyncio.create_task(
        asyncio.to_thread(controller.acquire_session_run, session_id)
    )
    try:
        run_id = await asyncio.shield(acquire_task)
    except asyncio.CancelledError as cancelled:
        try:
            run_id = await acquire_task
        except Exception:
            raise cancelled
        await asyncio.to_thread(
            controller.session_store.release_run, session_id, run_id
        )
        raise cancelled
    try:
        return run_id, StreamRunLeaseGuard(
            controller.session_store, session_id, run_id
        )
    except BaseException:
        await asyncio.to_thread(
            controller.session_store.release_run, session_id, run_id
        )
        raise


def get_controller(request: Request) -> AgentController:
    return request.app.state.controller


def _require_existing_user_session(request: Request, session_id: str) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.user_id == TEST_OWNER_USER_ID:
        return
    if request.app.state.session_store.get(session_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "会话不存在。"},
        )


@runtime_router.get("/runtime", response_model=RuntimeInfo)
def runtime_info(request: Request) -> RuntimeInfo:
    settings = request.app.state.settings
    return RuntimeInfo(
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        workspace=str(settings.resolved_workspace_dir()),
        command_execution_allowed=settings.allow_command_execution,
        database={"type": "sqlite", "status": "ready"},
    )


@router.post("/chat", response_model=ChatResponse)
def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    controller = get_controller(request)
    _require_existing_user_session(request, payload.session_id)
    try:
        return controller.handle_chat(payload)
    except SessionBusyError as exc:
        raise _session_busy_http_error(exc) from exc
    except SessionArchivedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_archived", "message": str(exc)},
        ) from exc
    except SessionLeaseLostError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_lease_lost", "message": "会话执行权已失效，请重试。"},
        ) from exc
    except UserMessageTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "message_too_large", "message": str(exc)},
        ) from exc


@router.post("/chat/stream")
async def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
    controller = get_controller(request)
    lease_guard: StreamRunLeaseGuard | None = None
    try:
        _require_existing_user_session(request, payload.session_id)
        controller.validate_request(payload)
        run_id, lease_guard = await _acquire_stream_run(
            controller, payload.session_id
        )
    except SessionBusyError as exc:
        raise _session_busy_http_error(exc) from exc
    except SessionArchivedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_archived", "message": str(exc)},
        ) from exc
    except UserMessageTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "message_too_large", "message": str(exc)},
        ) from exc
    try:
        response = LeaseStreamingResponse(
            controller.handle_chat_stream(
                payload,
                is_cancelled=request.is_disconnected,
                run_id=run_id,
                lease_guard=lease_guard,
            ),
            lease_guard=lease_guard,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )
        return response
    except BaseException:
        await lease_guard.close()
        raise


@router.post("/sessions")
def create_session(request: Request, payload: dict | None = None) -> dict[str, str]:
    requested = (payload or {}).get("session_id")
    session_id = requested if requested and request.state.principal.user_id == "00000000-0000-0000-0000-000000000001" else str(uuid4())
    request.app.state.session_store.get_or_create(session_id)
    return {"session_id": session_id}


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(request: Request, archived: bool = Query(default=False)) -> list[SessionSummary]:
    sessions = request.app.state.session_store.list_sessions(archived=archived)
    return [summarize_session(session) for session in sessions]


@router.get("/sessions/{session_id}", response_model=SessionInfo)
def get_session(request: Request, session_id: str) -> SessionInfo:
    session = request.app.state.session_store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "会话不存在。"},
        )
    return SessionInfo(
        session_id=session.session_id,
        display_title=session.display_title or session.session_id,
        archived_at=session.archived_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[MessageSchema.model_validate(item) for item in session.messages],
        tool_calls=session.tool_calls,
    )


def _validate_display_title(value: str) -> str:
    title = value.strip()
    if not 1 <= len(title) <= 80:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_session_title", "message": "标题长度必须为 1 到 80 个字符。"},
        )
    if any(unicodedata.category(character).startswith("C") for character in title):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_session_title", "message": "标题不能包含控制字符。"},
        )
    return title


def _session_info(session) -> SessionInfo:  # noqa: ANN001
    return SessionInfo(
        session_id=session.session_id,
        display_title=session.display_title or session.session_id,
        archived_at=session.archived_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[MessageSchema.model_validate(item) for item in session.messages],
        tool_calls=session.tool_calls,
    )


def _raise_session_mutation_error(exc: Exception) -> None:
    if isinstance(exc, SessionNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": str(exc)},
        ) from exc
    if isinstance(exc, SessionBusyError):
        code = "session_busy"
    elif isinstance(exc, SessionArchivedError):
        code = "session_archived"
    elif isinstance(exc, SessionOpenApprovalError):
        code = "session_open_approval"
    else:
        raise exc
    raise HTTPException(status_code=409, detail={"code": code, "message": str(exc)}) from exc


@router.patch("/sessions/{session_id}", response_model=SessionInfo)
def rename_session(
    request: Request, session_id: str, payload: SessionRenameRequest
) -> SessionInfo:
    try:
        session = request.app.state.session_store.rename_session(
            session_id, _validate_display_title(payload.display_title)
        )
    except (SessionNotFoundError, SessionBusyError, SessionArchivedError) as exc:
        _raise_session_mutation_error(exc)
    return _session_info(session)


@router.post("/sessions/{session_id}/archive", response_model=SessionInfo)
def archive_session(request: Request, session_id: str) -> SessionInfo:
    try:
        if request.app.state.approval_store.has_open_for_session(session_id):
            raise SessionOpenApprovalError("会话存在未处理审批，不能归档")
        session = request.app.state.session_store.set_archived(session_id, archived=True)
    except (
        SessionNotFoundError,
        SessionBusyError,
        SessionArchivedError,
        SessionOpenApprovalError,
    ) as exc:
        _raise_session_mutation_error(exc)
    return _session_info(session)


@router.post("/sessions/{session_id}/unarchive", response_model=SessionInfo)
def unarchive_session(request: Request, session_id: str) -> SessionInfo:
    try:
        session = request.app.state.session_store.set_archived(session_id, archived=False)
    except (SessionNotFoundError, SessionBusyError) as exc:
        _raise_session_mutation_error(exc)
    return _session_info(session)


@router.get("/approvals", response_model=list[ApprovalRequest])
def list_approvals(request: Request, status: str | None = Query(default=None)) -> list[ApprovalRequest]:
    if status is not None and status not in {"pending", "approved", "rejected", "consumed"}:
        raise HTTPException(status_code=400, detail="审批状态无效")
    return request.app.state.approval_store.list_requests(status=status)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalRequest)
def approve_request(request: Request, approval_id: str) -> ApprovalRequest:
    try:
        return request.app.state.approval_store.set_status(approval_id, "approved")
    except ApprovalMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalRequest)
def reject_request(request: Request, approval_id: str) -> ApprovalRequest:
    try:
        return request.app.state.approval_store.set_status(approval_id, "rejected")
    except ApprovalMutationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.post("/approvals/{approval_id}/resume/stream")
async def resume_approval_stream(
    request: Request, approval_id: str, payload: ApprovalResumeRequest
) -> StreamingResponse:
    controller = get_controller(request)
    lease_guard: StreamRunLeaseGuard | None = None
    try:
        approval = await asyncio.to_thread(
            request.app.state.approval_store.require_resumable, approval_id
        )
        session = await asyncio.to_thread(
            request.app.state.session_store.get, approval.session_id
        )
        if session is None:
            raise ApprovalResumeError(
                "approval_session_not_found", "审批对应的会话不存在。", status_code=404
            )
        run_id, lease_guard = await _acquire_stream_run(
            controller, approval.session_id
        )
    except ApprovalResumeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except SessionBusyError as exc:
        raise _session_busy_http_error(exc) from exc
    except SessionArchivedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_archived", "message": str(exc)},
        ) from exc
    try:
        response = LeaseStreamingResponse(
            controller.handle_approval_resume_stream(
                approval_id,
                payload,
                is_cancelled=request.is_disconnected,
                run_id=run_id,
                lease_guard=lease_guard,
            ),
            lease_guard=lease_guard,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )
        return response
    except BaseException:
        await lease_guard.close()
        raise


@router.get("/tools")
def list_tools(request: Request) -> list[dict]:
    controller = get_controller(request)
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "schema": tool.schema()["function"]["parameters"],
            "enabled": True,
        }
        for tool in controller.registry.tools()
    ]


@router.post("/collaborations", response_model=CollaborationInfo, status_code=201)
def create_collaboration(
    request: Request, payload: CollaborationCreateRequest
) -> CollaborationInfo:
    _require_existing_user_session(request, payload.session_id)
    return request.app.state.collaboration_store.create(payload)


@router.get("/collaborations", response_model=list[CollaborationSummary])
def list_collaborations(
    request: Request, session_id: str | None = Query(default=None, max_length=128)
) -> list[CollaborationSummary]:
    return request.app.state.collaboration_store.list(session_id=session_id)


@router.get("/collaborations/{collaboration_id}", response_model=CollaborationInfo)
def get_collaboration(request: Request, collaboration_id: str) -> CollaborationInfo:
    room = request.app.state.collaboration_store.get(collaboration_id)
    if room is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "collaboration_not_found", "message": "协作房间不存在。"},
        )
    return room


@router.post("/collaborations/{collaboration_id}/runs/stream")
async def stream_collaboration_run(
    request: Request, collaboration_id: str, payload: CollaborationRunRequest
) -> StreamingResponse:
    orchestrator = request.app.state.collaboration_orchestrator
    try:
        run = await asyncio.to_thread(orchestrator.acquire_run, collaboration_id, payload)
    except CollaborationBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "collaboration_busy", "message": "该协作房间已有运行中的任务。"},
        ) from exc
    except CollaborationNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "collaboration_not_found", "message": "协作房间不存在。"},
        ) from exc
    return StreamingResponse(
        orchestrator.stream_run(
            collaboration_id,
            payload,
            run,
            is_cancelled=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )
