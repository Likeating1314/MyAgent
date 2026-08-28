# MyAgent

MyAgent 是一个本地优先的 AI Agent 工作台，由 Vue 3 前端、Electron 桌面壳、FastAPI Python Agent Sidecar 和 Spring Boot 业务后台组成。

项目重点不是简单封装一次大模型调用，而是构建一套可观察、可审批、可恢复、可隔离的 Agent 执行系统：模型内容实时流式输出，工具参数由服务端校验，危险副作用必须经过人工审批，所有用户资源按已验证身份隔离。

## 项目亮点

- **完整 Agent 循环**：支持流式对话、结构化工具调用、上下文预算、最大执行步数、取消和事件持久化。
- **人在回路中的安全控制**：写文件和运行命令不会直接执行，而是先创建与会话、工具和规范化参数绑定的一次性审批。
- **多 Agent 协作**：支持 2–5 个 Agent 独立讨论，由协调者拆解任务和汇总结论，并通过租约与 fencing token 防止并发污染。
- **双 Token 安全边界**：Python Sidecar 同时验证本地进程 Token 和用户 Access JWT，不混用本地进程身份与用户身份。
- **用户级数据隔离**：会话、审批、协作房间、RAG 索引和工具工作区均按 `owner_user_id` 隔离。
- **完整认证 MVP**：Spring Boot 提供邮箱验证码注册、登录、RSA JWT、Refresh Token rotation、replay family revoke 和 JWKS。
- **桌面端安全存储**：Electron Access Token 仅保存在主进程内存，Refresh Token 使用系统 `safeStorage` 加密保存。
- **并发竞态防护**：认证 epoch、操作序号和响应校验可阻止旧用户的延迟 Refresh 或 API 响应覆盖新用户状态。

## 系统架构

```text
┌──────────────────────────────┐
│ Vue 3 + TypeScript            │
│ 登录注册 · 聊天 · 审批        │
│ 多 Agent 协作 · 设置          │
└──────────────┬───────────────┘
               │ REST + SSE
               │ 用户 JWT + 本地进程 Token
               ▼
┌──────────────────────────────┐       ┌──────────────────────────────┐
│ FastAPI Python Agent Sidecar  │       │ Spring Boot 业务后台          │
│ AgentController               │       │ 注册 · 登录 · 刷新 · 退出     │
│ ToolExecutor + 审批状态机      │       │ BCrypt · JWT · JWKS            │
│ 会话 · 协作 · RAG              │       │ PostgreSQL · Flyway             │
└──────────────┬───────────────┘       └──────────────┬───────────────┘
               │                                       │
               ▼                                       ▼
┌──────────────────────────────┐       ┌──────────────────────────────┐
│ SQLite Agent 运行时数据        │       │ PostgreSQL 认证数据            │
│ sessions · approvals          │       │ users · refresh_sessions       │
│ collaborations · RAG index    │       │ email verification challenges   │
└──────────────────────────────┘       └───────────────────────────────┘

Electron 桌面模式：
  主进程 ── 启动仅监听回环地址的 Sidecar ── 注入 X-Local-Agent-Token
  主进程 ── 保存内存 Access Token 与 safeStorage Refresh Token
```

### 一次 Agent 请求的执行流程

1. Renderer 通过类型化 API Client 发起聊天请求。
2. FastAPI 验证本地 Sidecar Token 和用户 JWT，并解析当前用户专属工作区。
3. Agent Controller 保存用户消息，从 SQLite 事件中按上下文预算组装模型输入。
4. 模型可以请求注册工具，`ToolExecutor` 使用 Pydantic Schema 验证工具参数和执行策略。
5. 只读工具可以直接执行；`write_file` 和 `run_command` 只创建审批，不立即产生副作用。
6. 工具事实被持久化，通过 SSE 推送到前端，并加入下一轮模型上下文。
7. 最终回答、取消或安全错误以唯一终态结束本次运行。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| Web 前端 | Vue 3、TypeScript、Vite |
| 桌面端 | Electron、Preload IPC、safeStorage、electron-builder |
| Agent 服务 | Python 3.11+、FastAPI、Pydantic、OpenAI 兼容 SDK、SQLite |
| 业务后台 | Java 21、Spring Boot 3、Spring Security、Spring Data JPA |
| 认证与密钥 | BCrypt 12、Nimbus JWT/JWK、RSA、Refresh Token Rotation |
| 数据库 | PostgreSQL、Flyway、SQLite |
| 流式协议 | Server-Sent Events（SSE） |
| 测试 | pytest、Spring Boot Test、Node Test Runner、vue-tsc |

## 认证与双 Token 边界

本地 Agent API 使用两个语义完全独立的凭据：

| 凭据 | 表达的身份 | 使用范围 |
| --- | --- | --- |
| `X-Local-Agent-Token` | 当前 Electron 启动的本地 Sidecar 进程 | `/api/runtime`，以及所有用户 API |
| `Authorization: Bearer <Access JWT>` | 当前登录用户 | Python 用户 Principal 和用户数据 API |

- `GET /health` 保持公开。
- `GET /api/runtime` 只验证本地进程 Token，便于 Electron 在登录前确认 Sidecar 身份。
- 其余用户数据 API 必须同时验证两个凭据。
- Python 不处理密码或登录，只通过 Spring JWKS 验证 RSA JWT。
- 校验包括 `iss`、`aud`、`exp`、`nbf`、`kid` 和 UUID `sub`。
- JWKS 使用有界 TTL 缓存；未知 `kid` 只重新加载一次，无法验证时 fail closed。

Access JWT 默认有效期为 10 分钟。Refresh Token 默认有效期为 30 天，使用安全随机数生成，数据库只保存 SHA-256 哈希。每次 Refresh 都会 rotation；旧 Token 被再次使用时，服务端撤销整个 Token Family。

浏览器优先使用 HttpOnly Cookie 保存 Refresh Token，Access Token 只存在于页面内存。Electron 的 Access Token 只存在于主进程内存，Refresh Token 使用 `safeStorage` 写入独立文件，并与模型 API Key 存储完全分离。

## 用户数据隔离

- sessions、approvals、collaborations 和 RAG documents 均带有 `owner_user_id`。
- 所有读取、更新、归档、执行、审批和续跑操作都必须携带已验证的用户 ID。
- 用户无权访问的资源统一返回 `404`，避免暴露资源是否存在。
- 新会话 ID 由服务端生成 UUID，不使用全局 `default` 会话。
- 工具执行目录固定为 `workspace/users/<validated-user-uuid>/`。
- 文件、Git、命令、搜索、索引和 RAG 操作均不能越出用户目录。
- `owner_user_id = NULL` 的旧数据保留但对登录用户隐藏，不会自动归属第一个用户。
- RAG 升级逻辑会删除路径用户 UUID 与记录 owner 冲突的历史污染索引，同时保留合法索引。
- 登录用户发生变化后，认证 epoch 会使旧请求、旧 Refresh 和延迟 JSON 响应失效，避免跨用户回填状态。

## 工具与审批模型

当前工具注册表包含：

- `read_file`：读取用户工作区文件
- `write_file`：写入文件，需要审批
- `list_files`：列出文件
- `search_text`：搜索文本
- `run_command`：运行白名单命令，默认关闭且需要审批
- `index_workspace`：建立当前用户的本地 RAG 索引
- `query_knowledge`：检索当前用户知识库
- `git_inspect`：结构化只读 Git 操作

所有工具参数都在服务端按 Schema 验证。路径在执行前会完成规范化，并验证其仍位于当前用户工作区内。

`write_file` 和 `run_command` 使用独立审批状态机：审批绑定会话、工具名称和规范化参数；批准后只能被原子消费一次。审批续跑提供 **at-most-once 工具尝试**，不会在进程崩溃或网络异常后自动重放可能产生副作用的操作。

## 多 Agent 协作

多 Agent 协作与普通聊天使用独立的编排器和状态存储。一个协作房间包含 2–5 个 Agent，并且必须有且只有一个协调者。

典型执行过程：

1. 协调者拆解用户任务。
2. 其他 Agent 按顺序分析并输出各自结论。
3. 后发言 Agent 从已持久化事件重新构造上下文。
4. 协调者读取所有成员结论并生成最终汇总。

协作运行使用租约、单调递增 fencing token 和原子终态写入。旧运行即使在被新运行接管后迟到，也不能继续写入事件或覆盖接管者状态。刷新或 SSE 断流后，前端从服务端快照恢复，并丢弃未持久化的临时 delta。

为降低风险，当前多 Agent 模式只开放只读工具，不允许写文件或执行命令。

## 项目结构

```text
backend/
  app/main.py                 FastAPI 入口与公开健康检查
  app/api/routes.py           聊天、会话、审批、工具和协作 API
  app/security.py             双 Token 与 JWT Principal 验证
  app/agent/controller.py     单 Agent 主循环与流式执行
  app/agent/collaboration.py  多 Agent 编排和租约处理
  app/services/               SQLite Store、RAG、租约和安全日志
  app/tools/                  文件、搜索、命令、Git 和 RAG 工具
  app/tests/                  安全、隔离、流式和生命周期测试

business-backend/
  src/main/java/.../auth/     JWT、Refresh、邮箱验证和 CORS
  src/main/java/.../user/     用户实体与 Repository
  src/main/resources/db/      Flyway 数据库迁移
  src/test/                    Spring 认证集成测试
  pom.xml · mvnw · mvnw.cmd    Java 21 Maven Wrapper 项目

frontend/
  src/api/                     浏览器 API Client 和单次 Refresh 重试
  src/components/              登录、聊天、审批、协作和设置组件
  src/stores/                  认证、会话、聊天和协作状态
  electron/                    主进程、IPC、Sidecar 生命周期、safeStorage
  test/                        Renderer、Electron、可访问性和竞态测试

docker-compose.yml             PostgreSQL、Spring、Python 和 Vite 服务
.env.example                   仅包含占位值的公开配置模板
```

## 使用 Docker Desktop 快速启动

### 环境要求

- Docker Desktop
- OpenSSL
- 可用的 SMTP 邮箱及授权码，用于发送注册验证码
- 可选：OpenAI 或兼容服务的 API Key；未配置时可使用项目的 Mock 路径进行部分本地演示

### 1. 创建本地配置

```powershell
Copy-Item .env.example .env
```

`.env` 已被 Git 忽略，不会上传到仓库。

### 2. 生成 JWT RSA 密钥

```powershell
New-Item -ItemType Directory -Force .secrets | Out-Null

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .secrets\jwt-private.pem
openssl pkey -in .secrets\jwt-private.pem -pubout -out .secrets\jwt-public.pem
```

`.env` 中保留以下文件引用即可。Docker Compose 会把密钥只读挂载到 Spring 容器：

```dotenv
JWT_PRIVATE_KEY_PEM=file:/run/secrets/jwt-private.pem
JWT_PUBLIC_KEY_PEM=file:/run/secrets/jwt-public.pem
```

### 3. 填写必要环境变量

至少配置：

```dotenv
BUSINESS_DATABASE_PASSWORD=<数据库强密码>
API_AUTH_TOKEN=<至少32字符的本地Sidecar Token>
EMAIL_VERIFICATION_CODE_SECRET=<独立随机密钥>

MAIL_HOST=<SMTP服务器>
MAIL_PORT=587
MAIL_USERNAME=<SMTP账号>
MAIL_PASSWORD=<SMTP授权码或应用专用密码>
MAIL_FROM=<发件邮箱>
```

`MAIL_PASSWORD` 通常应填写 SMTP 授权码或应用专用密码，而不是邮箱网页登录密码。

### 4. 启动 PostgreSQL 和业务后台

```powershell
docker compose up -d postgres business-backend
docker compose ps
```

首次启动需要下载 Maven 和 Python 依赖，耗时会比后续启动更长。

验证业务后台 JWKS：

```powershell
Invoke-WebRequest http://127.0.0.1:8081/.well-known/jwks.json -UseBasicParsing
```

### 5. 浏览器开发模式

浏览器模式需要额外启动 Python Agent 服务：

```powershell
docker compose up -d backend
```

然后启动前端：

```powershell
Set-Location frontend
npm install
npm run dev
```

浏览器访问：

```text
http://127.0.0.1:5173
```

### 6. Electron 开发模式

Electron 会自行启动并管理本地 Python Sidecar，因此使用 Electron 时不要再启动 Compose 中的 `backend` 服务。在 `frontend` 目录执行：

```powershell
npm run desktop:dev
```

Electron 不会自动启动 Spring 业务后台和 PostgreSQL，因此登录注册前仍需确保这两个服务已经运行。

### 停止服务

```powershell
docker compose stop frontend backend business-backend postgres
```

日常停止不要使用 `docker compose down -v`，因为 `-v` 会删除 PostgreSQL Volume 中的用户和 Refresh Session 数据。

## 不使用 Docker 的本地开发

### Python Agent

```powershell
Set-Location backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Spring Boot 业务后台

需要 Java 21 和可访问的 PostgreSQL。将 `.env.example` 中的业务后台配置注入当前 PowerShell 会话后执行：

```powershell
Set-Location business-backend
.\mvnw.cmd spring-boot:run
```

默认监听地址为：

```text
http://127.0.0.1:8081
```

生产配置缺少 RSA 密钥、邮箱验证码 Secret、发件地址或数据库连接时会 fail closed，这是预期的安全行为。

### Vue 前端

```powershell
Set-Location frontend
npm install
npm run dev
```

## 主要 API

### Spring Boot 业务后台（默认端口 `8081`）

- `POST /api/v1/auth/register/email-code`：发送注册邮箱验证码
- `POST /api/v1/auth/register`：注册并自动登录
- `POST /api/v1/auth/login`：登录
- `POST /api/v1/auth/refresh`：Refresh Token rotation
- `POST /api/v1/auth/logout`：撤销 Refresh Session
- `GET /api/v1/users/me`：获取当前用户资料
- `GET /.well-known/jwks.json`：公开 RSA 公钥集合

### Python Agent Sidecar（默认端口 `8000`）

- `GET /health`：公开健康检查
- `GET /api/runtime`：验证本地 Sidecar 身份
- `POST /api/chat`：同步聊天
- `POST /api/chat/stream`：SSE 流式聊天
- `GET/POST/PATCH /api/sessions/...`：会话管理
- `GET/POST /api/approvals/...`：审批和续跑
- `GET /api/tools`：工具市场与参数 Schema
- `POST/GET /api/collaborations/...`：多 Agent 协作

聊天 SSE 事件包括 `delta`、`tool_call`、`done`、`error` 和 `cancelled`。协作事件还会携带 collaboration、run、agent 和 round 标识，便于前端分别聚合多个 Agent 的输出。

## 测试与构建

### Spring Boot

```powershell
Set-Location business-backend
.\mvnw.cmd test
```

### Python

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m pytest
```

### 前端与 Electron

```powershell
Set-Location frontend
npm test
npm exec vue-tsc -- --noEmit
npm exec vite -- build --outDir ..\.tmp\vite-release-check --emptyOutDir
```

测试覆盖包括：

- BCrypt 密码哈希、统一登录错误、JWT Claims、JWKS、Refresh Rotation 与 Replay 撤销
- 双 Token 缺失或错误、JWT issuer/audience/expiry/sub 校验和 JWKS 缓存
- 用户 A/B 的会话、审批、协作、工具工作区和 RAG 隔离
- 旧版本 RAG 污染索引升级清理
- 延迟 Logout、旧 Refresh、认证 Epoch 变化和 401 单次刷新重试
- Electron IPC Sender 与参数校验、safeStorage 隔离和 Sidecar 进程所有权
- 登录注册表单语义、可访问性和 44px 交互目标
- SSE 断流恢复、租约接管、fencing token 和唯一终态

## 发布与安全说明

公开仓库不会提交以下内容：

- `.env` 和其他本地环境文件
- `.secrets/`、PEM 私钥和签名证书
- `data/` 下的 SQLite 运行数据库
- `workspace/` 下的用户文件和索引
- Python、Node 和 Maven 依赖缓存
- Electron `frontend/release*` 安装包与解包产物
- 日志、Refresh Token 文件和本地 API Key 文件

Windows 正式发布要求代码签名配置 fail closed。签名证书和密码只能通过构建环境秘密注入，不能写入仓库、日志或安装包资源。

## 当前 MVP 边界

当前登录系统用于识别用户并隔离本机数据，不提供跨设备 Agent 数据同步。以下能力计划在后续阶段实现：

- 邮箱变更后的重新验证与账户安全通知
- 找回密码和修改密码
- SSO / OAuth 登录
- 跨设备云同步
- 多实例共享限流
- 云端 Agent 任务和共享知识库
- 多 Agent 写操作的隔离工作树与合并流程

## 许可证

项目目前尚未选择开源许可证。在添加 LICENSE 前，公开可见不代表自动授权复制、修改或再发布。
