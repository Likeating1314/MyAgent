# MyAgent

MyAgent is a local-first AI workbench that combines a Vue 3 interface, an Electron desktop shell, a FastAPI Agent sidecar, and a Spring Boot business backend. It is designed to make agent execution visible and controllable: model output streams to the UI, tool calls are validated on the server, side effects require approval, and user data is isolated by authenticated owner.

## Why this project is interesting

- **Agent loop with real controls** — streaming chat, structured tool schemas, bounded execution steps, cancellation, and persisted conversation events.
- **Human-in-the-loop safety** — file writes and command execution create approval records first; approvals are single-use and tied to the exact session, tool, and normalized arguments.
- **Multi-agent collaboration** — independent collaboration rooms coordinate 2–5 agents with persisted events, leases, fencing tokens, and resumable SSE streams.
- **Security-first local architecture** — the Python sidecar requires both a local process token and a user JWT; user resources are scoped by `owner_user_id` and per-user workspace paths.
- **Production-minded authentication MVP** — Spring Boot issues short-lived RSA access JWTs and rotating refresh tokens; browsers use HttpOnly cookies and Electron uses OS-backed `safeStorage`.

## Architecture

```text
┌──────────────────────────────┐
│ Vue 3 + TypeScript            │
│ AuthGate · Chat · Approvals   │
│ Collaboration · Settings      │
└──────────────┬───────────────┘
               │ REST + SSE
               │ user JWT + local-agent token
               ▼
┌──────────────────────────────┐       ┌──────────────────────────────┐
│ FastAPI Python Agent sidecar  │       │ Spring Boot business backend  │
│ AgentController               │       │ Register / Login / Refresh    │
│ ToolExecutor + approval flow  │       │ BCrypt · JWT · JWKS            │
│ Session/Collaboration/RAG     │       │ PostgreSQL · Flyway             │
└──────────────┬───────────────┘       └──────────────┬───────────────┘
               │                                       │
               ▼                                       ▼
┌──────────────────────────────┐       ┌──────────────────────────────┐
│ SQLite runtime store          │       │ PostgreSQL auth store          │
│ sessions · approvals          │       │ users · refresh_sessions       │
│ collaborations · RAG index    │       │ email verification challenges   │
└──────────────────────────────┘       └───────────────────────────────┘

Electron desktop mode:
  Main process ── launches loopback sidecar ── injects X-Local-Agent-Token
  Main process ── owns access token memory and safeStorage refresh token
```

### Request and agent flow

1. The renderer sends a chat request through the typed API client.
2. FastAPI authenticates the local sidecar token and the user JWT, then resolves the user-owned workspace.
3. The agent controller persists the user message and builds a bounded context from SQLite events.
4. The LLM may request a registered tool. `ToolExecutor` validates its Pydantic arguments and checks policy.
5. Read-only tools execute immediately; `write_file` and `run_command` create an approval instead of performing a side effect.
6. Tool facts are persisted, streamed as SSE events, and fed back into the next model turn.
7. The run ends with a persisted final answer, cancellation, or a stable safe error code.

## Authentication and security boundaries

The project deliberately uses two independent credentials for local API requests:

| Credential | Meaning | Where it is accepted |
| --- | --- | --- |
| `X-Local-Agent-Token` | Identity of the current local sidecar process | `/api/runtime` and, together with JWT, user APIs |
| `Authorization: Bearer <Access JWT>` | Authenticated application user | User APIs and Python owner resolution |

`GET /health` is public. `GET /api/runtime` only checks the local process token so the Electron shell can verify a newly launched sidecar before login. All user data APIs require both credentials. Python never handles passwords or performs login; it validates Spring-issued RSA JWTs through the JWKS endpoint and fails closed when validation material is unavailable.

Access JWTs default to 10 minutes. Refresh tokens default to 30 days, are generated from cryptographically secure random bytes, and are stored in PostgreSQL only as SHA-256 hashes. Every refresh rotates the token. Reuse of an old token revokes the complete token family.

## User data isolation

- Every session, approval, collaboration, and RAG document is filtered by the authenticated `owner_user_id`.
- Cross-user access intentionally returns `404`, avoiding resource existence leaks.
- New sessions use server-generated UUIDs; there is no global `default` session.
- Tool execution is rooted at `workspace/users/<validated-user-uuid>/`.
- File, Git, command, search, and RAG operations cannot escape that directory.
- Historical rows with `owner_user_id = NULL` remain hidden and are never automatically assigned to the first user.
- The RAG upgrade cleanup removes known old index rows whose path user UUID conflicts with their owner, while retaining valid indexes.
- Authentication state changes use an epoch/operation guard so delayed responses cannot repopulate a newer user’s stores.

## Repository layout

```text
backend/
  app/main.py                 FastAPI application and public health endpoint
  app/api/routes.py           Chat, session, approval, tool, and collaboration APIs
  app/security.py             Dual-token and JWT principal dependencies
  app/agent/controller.py     Single-agent loop and streaming execution
  app/agent/collaboration.py  Multi-agent orchestration and lease handling
  app/services/               SQLite stores, RAG, leases, and safe logging
  app/tools/                  File, search, command, Git, and RAG tools
  app/tests/                  Security, isolation, streaming, and lifecycle tests

business-backend/
  src/main/java/.../auth/     JWT, refresh rotation, email verification, CORS
  src/main/java/.../user/     User entity and repository
  src/main/resources/db/      Flyway migrations
  src/test/                    Spring authentication integration tests
  pom.xml · mvnw · mvnw.cmd    Java 21 / Maven Wrapper project

frontend/
  src/api/                     Browser API client and one-refresh retry logic
  src/components/              AuthGate, chat, approvals, collaboration, settings
  src/stores/                  Auth, session, chat, collaboration, and request state
  electron/                    Main process, preload bridge, sidecar lifecycle, safeStorage
  test/                        Renderer, Electron, accessibility, and race-condition tests

docker-compose.yml             PostgreSQL, Spring backend, Python sidecar, and Vite services
.env.example                   Public configuration template with placeholders only
```

## Quick start with Docker Desktop

Requirements: Docker Desktop, a working SMTP account for registration codes, and OpenSSL for local JWT keys.

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force .secrets | Out-Null

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .secrets\jwt-private.pem
openssl pkey -in .secrets\jwt-private.pem -pubout -out .secrets\jwt-public.pem
```

Keep these values in `.env`; Compose mounts the files read-only into the Spring container:

```dotenv
JWT_PRIVATE_KEY_PEM=file:/run/secrets/jwt-private.pem
JWT_PUBLIC_KEY_PEM=file:/run/secrets/jwt-public.pem
```

Set at least the following values in `.env`:

```dotenv
BUSINESS_DATABASE_PASSWORD=<strong-local-database-password>
API_AUTH_TOKEN=<at-least-32-character-local-sidecar-token>
EMAIL_VERIFICATION_CODE_SECRET=<independent-random-secret>
MAIL_HOST=<smtp-host>
MAIL_PORT=587
MAIL_USERNAME=<smtp-account>
MAIL_PASSWORD=<smtp-app-password-or-authorization-code>
MAIL_FROM=<sender-address>
```

Start the business backend and sidecar:

```powershell
docker compose up -d postgres business-backend backend
docker compose ps
```

Verify the Spring public key endpoint:

```powershell
Invoke-WebRequest http://127.0.0.1:8081/.well-known/jwks.json -UseBasicParsing
```

Start the browser client in another terminal:

```powershell
Set-Location frontend
npm install
npm run dev
```

Or start the Electron development shell:

```powershell
npm run desktop:dev
```

Stop services without deleting the PostgreSQL volume:

```powershell
docker compose stop business-backend backend postgres
```

Do not use `docker compose down -v` for routine shutdown; it removes the database volume and authentication data.

## Local development without Docker

### Python Agent

```powershell
Set-Location backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Spring business backend

Use Java 21 and a reachable PostgreSQL instance. Set the variables from `.env.example` in the current shell, then run:

```powershell
Set-Location business-backend
.\mvnw.cmd spring-boot:run
```

The backend listens on `http://127.0.0.1:8081` by default. Production configuration fails closed when RSA keys, the email verification secret, sender address, or database connection are missing.

### Frontend checks

```powershell
Set-Location frontend
npm test
npm exec vue-tsc -- --noEmit
npm exec vite -- build --outDir ..\.tmp\vite-release-check --emptyOutDir
```

## API surface

### Spring business backend (`:8081`)

- `POST /api/v1/auth/register/email-code`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `GET /api/v1/users/me`
- `GET /.well-known/jwks.json`

### Python Agent sidecar (`:8000`)

- `GET /health`
- `GET /api/runtime`
- `POST /api/chat` and `POST /api/chat/stream`
- `GET/POST/PATCH /api/sessions/...`
- `GET/POST /api/approvals/...`
- `GET /api/tools`
- `POST/GET /api/collaborations/...`

The streaming endpoints use Server-Sent Events. Chat events include `delta`, `tool_call`, `done`, `error`, and `cancelled`; collaboration events additionally identify the room, run, agent, and round.

## Built-in tools and approval model

The tool registry includes:

- `read_file`, `write_file`, `list_files`, `search_text`
- `run_command` (disabled by default and policy-gated)
- `index_workspace`, `query_knowledge`
- `git_inspect` (structured read-only status/diff/log/show/branch operations)

The server validates every tool argument against its schema. Paths are normalized and constrained to the authenticated user workspace. `write_file` and `run_command` require a pending approval. Approval consumption is atomic and at-most-once; a process crash after consumption is reported as an uncertain outcome rather than silently replayed.

## Testing

The repository includes regression coverage for:

- password hashing, generic login errors, JWT claims, JWKS, refresh rotation/replay, logout, expiry, and rate limits;
- missing/invalid dual credentials, issuer/audience/expiry/sub validation, JWKS cache behavior;
- user A/B isolation for sessions, approvals, collaborations, workspaces, tools, and RAG, including legacy index cleanup;
- delayed logout, stale refresh responses, epoch changes, single 401 refresh/retry, and user-switch state invalidation;
- Electron IPC sender/argument validation, safeStorage separation, sidecar ownership, CSP, and release smoke checks;
- semantic auth forms, accessible labels/errors, 44px interaction targets, TypeScript checking, and production builds.

## Security and privacy notes

The public repository intentionally excludes `.env`, `.secrets/`, runtime `data/`, user `workspace/`, dependency caches, logs, and Electron `frontend/release*` packaging output. Generate local secrets from `.env.example`; never commit SMTP credentials, API keys, signing certificates, refresh-token files, or JWT private keys.

This MVP is local-first. Login provides identity and local data separation, not cross-device synchronization. Email verification, password reset, SSO, shared cloud storage, distributed rate limiting, and cloud Agent synchronization are planned follow-up capabilities rather than part of this release.

## License

No license has been selected yet. Until a license is added, the repository contents should be treated as “all rights reserved”; public visibility does not by itself grant reuse rights.
