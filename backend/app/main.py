from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import SERVICE_NAME, SERVICE_VERSION
from app.api.routes import router, runtime_router
from app.agent.controller import AgentController
from app.agent.collaboration import CollaborationOrchestrator
from app.config import get_settings
from app.models.schemas import HealthResponse
from app.services.approval_store import ApprovalStore
from app.services.logger import configure_logging
from app.services.rag_store import RagStore
from app.services.session_store import SQLiteSessionStore
from app.services.collaboration_store import CollaborationStore
from app.security import JwksVerifier, is_bootstrap_origin_allowed, is_loopback_request


def create_app(settings=None) -> FastAPI:
    configure_logging()
    settings = settings or get_settings()
    app = FastAPI(title="MyAgent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.jwt_verifier = JwksVerifier(url=settings.jwks_url, issuer=settings.jwt_issuer, audience=settings.jwt_audience, ttl_seconds=settings.jwks_cache_seconds)
    database_path = settings.resolved_sqlite_path()
    app.state.session_store = SQLiteSessionStore(database_path)
    app.state.approval_store = ApprovalStore(database_path, ttl_seconds=settings.approval_ttl_seconds)
    app.state.rag_store = RagStore(database_path, settings.resolved_workspace_dir())
    app.state.collaboration_store = CollaborationStore(database_path)
    app.state.controller = AgentController(
        app_settings=settings,
        session_store=app.state.session_store,
        approval_store=app.state.approval_store,
        rag_store=app.state.rag_store,
    )
    app.state.collaboration_orchestrator = CollaborationOrchestrator(
        app_settings=settings,
        store=app.state.collaboration_store,
        rag_store=app.state.rag_store,
    )
    app.include_router(runtime_router)
    app.include_router(router)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(service=SERVICE_NAME, version=SERVICE_VERSION)

    @app.get("/auth/token")
    def bootstrap_token(request: Request, response: Response) -> dict[str, str]:
        client_allowed = settings.allow_non_loopback_token_bootstrap or is_loopback_request(request)
        if not client_allowed or not is_bootstrap_origin_allowed(request):
            raise HTTPException(status_code=403, detail="令牌只提供给受信任的本地前端")
        response.headers["Cache-Control"] = "no-store"
        return {"token": settings.api_auth_token}

    return app


app = create_app()
