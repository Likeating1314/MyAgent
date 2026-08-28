# MyAgent

> Public repository note: local `.env` files, `.secrets/`, runtime databases under `data/`, per-user `workspace/` data, dependency caches, and Electron `frontend/release*` packaging output are intentionally excluded from version control. Generate credentials locally from `.env.example`; never commit production tokens, SMTP credentials, signing certificates, or JWT private keys.

## 用户认证与双 Token 边界

新增的 `business-backend/` 使用 Java 21、Spring Boot、PostgreSQL 与 Flyway 管理用户、BCrypt 密码、10 分钟 RSA Access JWT 和 30 天 rotation Refresh Token。刷新凭据在数据库中只保存 SHA-256；旧 token replay 会撤销整个 family。浏览器使用 HttpOnly Cookie，Electron 则把 Refresh Token 通过 `safeStorage` 写入独立的 `auth/refresh-token.bin`，与 `credentials/api-key.bin` 完全分离。Access Token 只存在于 Electron 主进程或浏览器页面内存，绝不写 localStorage/sessionStorage。

本地 Python sidecar 使用两种不可互换的身份：`X-Local-Agent-Token` 证明请求来自本次 Electron sidecar 进程，`Authorization: Bearer <Access JWT>` 证明当前用户。`/health` 公开；`/api/runtime` 只检查本地进程 token；其余 `/api` 同时检查两者。Python 只通过 Spring JWKS 验证用户 JWT，不提供密码或登录接口。业务后台不可用且没有有效 JWKS 缓存时会 fail closed。

SQLite 的 sessions、approvals、collaborations 与 RAG 文档均增加 `owner_user_id` 范围；工具工作区固定为 `workspace/users/<validated-user-uuid>/`。RAG 的生产单例只共享数据库信息，每次索引与查询都会重新绑定 `ToolContext.workspace_dir`，不会遍历总 workspace 或其他用户目录。升级时会删除旧版本产生的明确跨用户污染行：路径形如 `users/<path-user-uuid>/...` 且该 UUID 与 `owner_user_id` 不一致；合法历史索引和 `owner_user_id=NULL` 数据不会因此被认领或改写。跨用户读取、修改、归档、运行、审批或协作访问统一表现为 404。升级前已有的 `owner_user_id=NULL` 行保留但对真实登录用户隐藏，不会自动归属第一个用户；后续可通过单独迁移工具导入。本阶段数据仍只在当前设备，登录不提供跨设备 Agent 数据同步，离线时已有 Access Token 过期后需要业务后台恢复才能继续。

刷新流程：登录/注册返回短期 Access Token并创建 Refresh Session；每次 refresh 生成新 Refresh Token 并原子撤销旧 token；401 客户端最多执行一次 refresh + retry，失败后自动停止聊天/协作流、清空所有用户 Store，并清除 Electron 主进程 Access Token、加密 Refresh Token 文件或浏览器 HttpOnly Cookie。所有普通 API 请求和流式请求都绑定发起时的认证代次；响应返回时代次已变化则丢弃，禁止在下一用户登录后回填状态。显式退出在等待 Electron IPC 或浏览器 logout 网络请求之前就同步清空认证、凭据和用户数据。

MyAgent 是一个本地可运行的智能工作台，前端使用 Vue 3 + Vite + TypeScript，Agent 服务使用 FastAPI + Python。

## 架构

```text
Vue 前端
  -> FastAPI
  -> Agent Controller
  -> LLM Client
  -> Tool Executor
  -> 工具返回结果
  -> 再次交给 LLM
  -> 最终答案返回前端
```

后端默认使用 OpenAI Python SDK；如果没有配置 `OPENAI_API_KEY`，会退回到 mock 模式，方便本地演示。
也可以在前端设置面板中选择模型服务商，输入自己的接口密钥、接口地址和模型名；这些设置会随聊天请求发送给后端，用于切换 Agent 实际接入的大模型。

OpenAI 工具定义不声明 `strict=true`。注册工具的 Pydantic JSON Schema 会直接发送给 OpenAI 兼容接口，实际工具参数仍由 `ToolExecutor` 使用对应 Pydantic 模型在服务端验证；非法参数只会生成 `status=error` 的工具记录，不会绕过参数校验执行工具。

## 项目结构

```text
backend/
  app/main.py              # FastAPI 入口
  app/config.py            # 环境变量与运行配置
  app/api/routes.py        # REST API
  app/agent/controller.py  # Agent 主循环
  app/agent/collaboration.py # 独立多 Agent 协作编排器
  app/agent/llm_client.py  # LLM 调用封装
  app/agent/tool_registry.py
  app/agent/tool_executor.py
  app/tools/*.py           # 基础工具
  app/services/*.py        # 会话、协作、租约与日志
  app/tests/*.py           # pytest 测试

frontend/
  src/App.vue              # 应用入口
  src/api/client.ts        # 后端请求封装
  src/stores/chat.ts       # 聊天状态
  src/stores/collaboration.ts # 独立协作状态
  src/components/*.vue     # 聊天、设置、工具记录
```

## 一次命令的流程

1. 用户在前端输入任务。
2. 前端默认调用 `POST /api/chat/stream`，使用模型 token 级流式输出。
3. 后端将用户消息追加到 SQLite 事件表，并按上下文预算组装 system prompt 和最近历史。
4. `AgentController` 调用 `LLMClient`。
5. 如果模型返回工具调用，`ToolExecutor` 执行工具。
6. 工具结果写回上下文，再次调用模型。
7. 直到模型输出最终回答或达到最大步数。
8. 后端把答案和工具记录返回前端展示。

## 工具

第一版实现了：

- `read_file`
- `write_file`
- `list_files`
- `search_text`
- `run_command`
- `index_workspace`
- `query_knowledge`
- `git_inspect`

其中 `run_command` 默认关闭，只允许白名单命令，且拒绝危险操作。客户端只能进一步关闭命令权限，不能越过服务端的 `ALLOW_COMMAND_EXECUTION=false`。
`run_command` 和 `write_file` 都必须先生成审批记录；审批绑定会话、工具和规范化参数，默认 15 分钟过期，批准后只能原子消费一次。文件审批会返回规范化路径、变更类型、原内容摘要和统一 diff，新建文件同样不能静默落盘。工具不会因轮询、应用启动或页面刷新自动执行；只有用户明确点击“批准并继续”后，前端才调用续跑 API。

### Git 只读工具范围

`git_inspect` 使用按 `operation` 判别的结构化参数模型，不接受 `args: list[str]` 或任何自由 Git 参数。所有操作的 `cwd` 都必须位于 workspace 内，并且仓库根目录、Git 元数据/common dir、主对象目录及 alternate 对象目录也必须位于 workspace 内；HTTP alternates 被拒绝。

- `status`：`format` 仅支持 `short`、`porcelain-v1`、`porcelain-v2`、`long`；可选择是否显示 branch，并把未跟踪文件范围限制为 `no`、`normal` 或 `all`。
- `diff`：只支持受校验的 `from_revision`/`to_revision`、`staged`、仓库内具体相对路径，以及 `patch`、`stat`、`name-only`、`name-status` 等展示方式。路径会转换成工具生成的 top-level literal pathspec。
- `log`：只支持仓库内 revision、仓库内具体路径、`max_count`、`since`/`until` 和预定义日志格式。
- `show`：只支持受校验的对象名、仓库内具体路径、commit/file 模式和预定义展示格式。
- `branch`：只能 `list` 或 `show-current`；list 可选择展示全部引用或详细信息，不能携带名称或修改参数。

工具明确不支持 `--output`、`--no-index`、`--ext-diff`、`--textconv`，也不支持 branch 创建、删除、移动、复制、强制修改或设置上游。绝对路径、`..`、选项形态路径和 pathspec magic 会被拒绝。执行固定使用 `shell=False`、关闭 stdin/pager/颜色/外部 diff/textconv/fsmonitor，设置 `GIT_OPTIONAL_LOCKS=0`，并在并发排空输出时只保留配置的最大字符数。

## 本地 API 安全边界

- `/api/runtime` 只要求 `X-Local-Agent-Token`；`/api/chat`、`/api/sessions`、`/api/tools`、`/api/approvals` 及其他用户数据 `/api` 路由同时要求 `X-Local-Agent-Token` 与 `Authorization: Bearer <Access JWT>`；`/health` 保持公开。
- 未显式配置 `API_AUTH_TOKEN` 时，后端每次启动生成随机令牌。受信任的回环前端来源可通过 `/auth/token` 引导令牌，前端只保存在当前页面内存并自动附加到请求，不写日志或 localStorage。
- 默认 CORS 仅允许 `http://127.0.0.1:5173`、`http://localhost:5173` 和 Electron 的 `null` 本地来源。`null` 来源不能调用令牌引导端点；Electron 由主进程在网络层附加令牌。
- 外部脚本或固定令牌集成应设置至少 32 个字符的 `API_AUTH_TOKEN`。不要把该值提交到仓库、前端构建变量或日志。

启动令牌主要阻止网页跨站请求和意外暴露，不用于抵御已经能以当前用户身份运行、读取进程内存或控制本机网络的恶意程序。若显式将 CORS 或监听地址放宽到局域网，应在外层增加可信网络隔离和正式认证。

### 扩展能力

当前已加入 README 扩展清单中的这些能力：

- 流式输出：`POST /api/chat/stream` 使用 `AsyncOpenAI` 原生异步流，SSE 返回 `delta`、`tool_call`、`done`、`error`、`cancelled` 事件；同步 `POST /api/chat` 保持可用。阻塞型工具和流式路径中的 SQLite 操作在线程池执行，不阻塞 FastAPI 事件循环。
- 多会话持久化：`GET /api/sessions` 默认返回活跃会话，`GET /api/sessions?archived=true` 返回归档会话，`GET /api/sessions/{session_id}` 返回完整会话。
- SQLite：默认数据库路径为 `./data/agent.sqlite3`，可用 `SQLITE_PATH` 修改。
- RAG 知识库：`index_workspace` 索引工作区文本文件，`query_knowledge` 检索本地知识片段。
- Git 工具：`git_inspect` 只允许结构化的 `status`、`diff`、`log`、`show`、`branch` 只读操作，不透传 Git 参数。
- 用户审批系统：`GET /api/approvals?status=pending` 查看待审批项，`POST /api/approvals/{id}/approve` 或 `reject` 更新状态；批准后由显式 `resume/stream` 请求执行原审批工具并继续 Agent。
- 插件化工具市场：`GET /api/tools` 返回当前注册工具、说明和参数 schema，前端右侧工具市场展示。
- Docker 部署：`docker-compose.yml` 可同时启动后端和前端，并挂载本地 SQLite 数据库。

仍需接入外部服务或更完整运行时的能力：

- PostgreSQL：当前已保留 SQLite 存储抽象，后续可新增 PostgreSQL Store 实现。
- 浏览器自动化工具：建议后续基于 Playwright 增加可控浏览器会话。
- 多 Agent 写入与命令审批：当前协作 MVP 只允许只读工具，第二阶段再评估隔离工作树、变更合并与独立审批状态机。

## 多 Agent 对话协作 MVP

协作入口与普通聊天相互独立。`CollaborationOrchestrator` 不复用 `AgentController` 的聊天循环或审批续跑；`CollaborationStore` 在同一 SQLite 文件中维护 `collaborations`、`collaboration_agents`、`collaboration_runs`、`collaboration_events` 和 `collaboration_leases`。表和索引使用幂等初始化，可与既有会话表共同升级。

一个房间包含 2–5 位 Agent，必须且只能有一个 coordinator。Agent 保存 `id`、`name`、`role`、`prompt`、`position`、`is_coordinator`；用户 prompt 只作为不可覆盖安全 prompt 之后的角色补充。默认并且当前最多两轮：第一轮协调者先拆解、其他成员按 position 分析；第二轮成员阅读此前已经完整持久化的消息后回应，协调者最后综合。后发言者的上下文每次都从 `collaboration_events` 重新构造，因此不会依赖仅存在于 SSE delta 或浏览器内存的内容。同一房间可继续发送后续用户消息，历史 run 的用户消息、Agent 完整结论和只读工具事实都会进入后续上下文。

协作 API：

- `POST /api/collaborations`：创建房间并验证人数、唯一 coordinator、字段长度和轮数。
- `GET /api/collaborations?session_id=...`：列出关联普通 session 的房间摘要。
- `GET /api/collaborations/{collaboration_id}`：返回 Agent、run 和完整持久化事件快照，供刷新与断流恢复。
- `POST /api/collaborations/{collaboration_id}/runs/stream`：为一条用户消息创建 run 并以 SSE 执行两轮讨论。

协作 SSE 事件包括 `run_started`、`agent_status`、`agent_delta`、`agent_message`、`agent_tool_call`、`round_completed`、`done`、`error`、`cancelled`。Agent 事件包含 `collaboration_id`、`run_id`、`agent_id`、`agent_name`，以及适用的 `message_id` 和 `round`。`agent_delta` 仅用于实时显示，不写 SQLite；完整 `agent_message` 与 `agent_tool_call` 在发出对应事件前持久化。前端按 `message_id` 分别聚合不同 Agent 的增量，收到完整消息后用服务端事实替换临时文本。

每个 collaboration 同时只有一个未过期 lease。创建 run 时生成单调递增 fencing token；所有普通事件和终态写入都在事务中校验 `collaboration_id + run_id + fencing_token`。并发请求返回 HTTP `409 collaboration_busy`，旧 run 即使迟到也不能继续写普通事件。若运行中发现 lease 已删除、过期、续租失败或被接管，服务端通过旧 run 行上的 `run_id + fencing_token + running + 空终态` 条件将它原子标记为 `error`，只清理仍匹配旧 token 的 lease，并发送唯一 `collaboration_lease_lost` SSE；接管者的新 lease 不会被修改。取消和失租同时发生时也遵循该规则：有效 lease 产生 `cancelled`，取消终结发现 lease 已失效则回退为持久化的 `error` 与唯一 `collaboration_lease_lost` SSE，不会留下 `running`。`done`、`error`、`cancelled` 通过 run 的空终态条件更新保证最多一个。运行中可停止；取消后不会启动下一 Agent、模型调用或工具。已经开始的同步只读工具允许完成并保存事实，随后停止。错误只返回稳定 code 和安全文案，不保存或返回 API Key、Authorization、完整模型原始响应或异常堆栈。

协作模式的工具集合由服务端从注册表重新构造，只包含 `read_file`、`list_files`、`search_text`、`index_workspace`、`query_knowledge`、`git_inspect`。客户端 settings 中即使声明命令权限也会被服务端强制关闭；`write_file` 和 `run_command` 不会进入模型工具 schema，也不能进入执行器。当前不支持多 Agent 写入、命令执行或复用普通聊天审批续跑。

刷新、`error`、`cancelled` 或 `unexpected_eof` 后，前端重新 GET 房间快照，并丢弃所有未持久化 delta。协作使用自己的 `AbortController`，停止协作不会取消普通聊天；普通聊天、审批和会话交互保持原样。

### 新增 API

- `POST /api/chat/stream`
- `GET /api/runtime`
- `GET /api/sessions`
- `PATCH /api/sessions/{session_id}`
- `POST /api/sessions/{session_id}/archive`
- `POST /api/sessions/{session_id}/unarchive`
- `GET /api/approvals`
- `POST /api/approvals/{approval_id}/approve`
- `POST /api/approvals/{approval_id}/reject`
- `POST /api/approvals/{approval_id}/resume/stream`
- `GET /api/tools`
- `POST /api/collaborations`
- `GET /api/collaborations`
- `GET /api/collaborations/{collaboration_id}`
- `POST /api/collaborations/{collaboration_id}/runs/stream`

### 流式事件和取消语义

`POST /api/chat/stream` 的 SSE 事件均使用 JSON `data`：

- `delta`：新增的 assistant 文本片段，负载为 `{ "content": "..." }`。
- `tool_call`：一个已经执行并持久化的工具记录。
- `done`：仅表示本次 Agent 运行正常结束，负载为完整 `ChatResponse`；审批续跑中的获批工具仍可能返回持久化的 `error` 记录，不能仅凭 `done` 判断工具成功。
- `error`：任务失败，负载只包含稳定 `code`、可展示的 `message` 和可选 `session_id`，不包含异常堆栈、请求头、密钥或完整模型响应。
- `cancelled`：仅表示用户取消或客户端连接终止；不会同时发送 `done` 或 `error`。

前端为每次流请求创建新的 `AbortController`，运行期间提供“停止”按钮。取消或失败后会重新读取服务端会话，移除临时空 assistant 消息并恢复已持久化的工具记录。用户消息在后端开始处理时立即保存，即使后续模型失败或取消也会保留；未完成的 assistant 流片段不会写入会话。若同步工具已经开始，取消会等待该工具完成并保存结果，但不会再启动后续工具或下一轮模型请求。客户端断开会关闭当前模型流，并停止后续处理。

### 审批续跑状态机

`POST /api/approvals/{approval_id}/resume/stream` 的请求体只接受当前 `AgentSettings`。`session_id`、`tool_name` 和规范化工具参数全部从服务端审批记录读取，`approval_id` 由 Controller 注入，客户端不能提交或覆盖待执行参数。服务端先验证审批为未过期的 `approved`，再获取对应 session 租约；每个追加事件继续携带 `run_id` fencing token。续跑会生成新的唯一 `tool_call_id`，原子追加 assistant tool-call、工具记录和 tool message，不新增或重复用户消息，然后继续模型循环直到最终回答或最大步数。SSE 与聊天流使用相同的 `delta`、`tool_call`、`done`、`error`、`cancelled` 协议和单一终态规则。

`approval_id` 不出现在模型可见的工具 Schema 中。普通 Agent 工具调用只要自行携带该字段就会被拒绝且不会消费审批；只有显式续跑 Controller 可以调用受信任注入路径。因此单独批准审批后再发送普通聊天，也不能触发已批准的副作用工具。

稳定拒绝码包括 `approval_not_found`、`approval_pending`、`approval_rejected`、`approval_expired`、`approval_invalid_state`、`approval_consumed`、`approval_replaced`、`approval_session_not_found`、`session_busy`、`session_archived` 和 `session_lease_lost`。approve/reject API 均返回 `{detail: {code, message}}`，前端不会展示原始 JSON detail。双击或并发续跑请求由 session 租约与审批原子消费共同保护；已经消费的审批绝不自动重试。

相同 `session_id`、`tool_name` 和规范化参数的未过期 `pending` 审批会在 `BEGIN IMMEDIATE` 事务中复用，避免文件目标变化或重复点击持续生成相同审批。若文件哈希等执行事实变化导致原 approved 审批失败，新 pending 会通过结构化 `replacement_approval_id` 关联回旧审批；旧审批随后返回 `approval_replaced`，界面只允许操作新的 pending，不从异常文本解析审批编号。审批表通过幂等加列保存可选的 `last_resume_outcome=cancelled`。该字段的清空和写入都在 SQLite 事务中校验相同 `session_id + run_id` 的未过期 `session_runs` 租约；旧 run 被接管后不能迟到清空或写入，新续跑开始时也只能由当前 run 清空。

前端审批状态由刷新后的审批记录和该审批最新的持久化工具记录推导。优先级是：当前请求的瞬时 `running`；稳定的 `rejected`；`consumed + 最新 ok` 为“已完成”；`consumed + error/无记录` 为“结果不确定”；未消费且已过期为“已过期”；未消费且最新记录为 `error` 时为“工具未成功/需要重试或重新审批”；随后才是持久化取消；其余 `pending` 或 `approved` 为“等待”。因此 `last_resume_outcome` 不会覆盖 consumed、工具记录、rejected 或 expired 等更强事实。会话重命名、归档、恢复和审批拒绝各自使用资源级 pending 锁与就地错误提示，不会借用 Agent 全局 loading 阻塞无关浏览。

审批续跑提供的是 **at-most-once 工具尝试**，不承诺外部副作用 exactly-once。审批在工具副作用前被原子消费；若进程在消费后、工具结果持久化前崩溃，该结果属于“不确定”，再次点击会得到 `approval_consumed`，不会自动重放命令或文件写入。`request.is_disconnected`、协程 `Task.cancel()` 和异步生成器关闭都会进入相同清理路径：尚无 consumed/工具记录等更强事实时，当前 run 尝试持久化 `last_resume_outcome=cancelled`；同步工具若已经启动则允许完成并先保存工具事实，取消不会覆盖该事实，也不会再启动模型或工具。model/tool/session 错误和 lease-lost 不会被误标为 cancelled。已经启动的同步工具也无法由取消、lease-lost 或进程内 fencing 撤销；系统只能阻止后续工具、模型调用及旧 run 的事件写入。前端在错误、取消和意外 EOF 后重新同步会话与审批，并据持久化工具记录区分已完成与结果不确定。

### 会话事件、迁移和并发

会话的新写入使用 `session_events` 追加表，字段包括 `session_id`、会话内严格递增的 `sequence`、`event_type`、`payload_json` 和 `created_at`。`event_type` 区分 `message` 与 `tool_call`；assistant tool-call、对应工具记录和 tool message 在一个 SQLite 事务中批量追加。API 仍返回原来的 `messages` 和 `tool_calls` 数组。

启动时会在单个事务中把旧 `sessions.messages_json` 和 `tool_calls_json` 导入事件表，并在 `session_event_migrations` 按 session 记录完成状态。迁移可重复执行且不会重复导入；旧 JSON 损坏会记录 session 标识并阻止启动，不会静默跳过。旧列不会删除，但新消息不会再回写旧 JSON。升级前应备份 SQLite 文件；直接回滚到只读取旧 JSON 的旧版本会看不到升级后新增的会话事件，需要先使用兼容版本导出或回填。

会话元数据通过幂等迁移在原 `sessions` 表增加 `display_title` 和 `archived_at`；旧会话的显示标题初始化为不可变的 `session_id`，不会通过重命名主键。标题 trim 后必须为 1–80 个字符且不能含控制字符。运行中的会话不能重命名或归档；存在 `pending`/`approved` 审批的会话不能归档。归档会话仍可读取，但不能聊天、续跑、追加事件或修改标题，必须先取消归档。当前阶段不提供永久删除。

同一 session 采用 SQLite `session_runs` 租约：同步与流式请求都只能同时持有一个 run，冲突请求返回 HTTP `409` 和 `session_busy`。租约默认 120 秒并由运行中的 Controller 定期续期；心跳续租返回 false 或抛出异常时会 fail-closed，并通过线程安全事件传播到当前执行循环。Controller 在每轮模型调用前后检查该状态，并在启动每个工具前主动续租；一旦租约不可确认，就不再启动模型或工具。同步请求返回 HTTP `409 session_lease_lost`，流式请求只发送一个 `error` 终态且 code 为 `session_lease_lost`。

租约会在成功、错误、取消和断开清理时按当前 `run_id` 条件释放，因此旧 run 的清理不会删除接管者的新租约；进程异常退出后，过期租约可被下一请求接管。每次 run 内事件追加还会在同一事务中校验 `run_id` fencing token，已被接管的旧 run 不能晚到写入。不同 session 不共享租约，可以并发调用模型和工具。模型或工具运行期间不持有 SQLite 写事务。已经开始的同步工具无法撤销：若执行期间租约丢失，会等待工具结束并拒绝旧 run 的工具记录写入，也不会启动后续步骤，但工具已经产生的文件、命令或其他外部副作用可能仍然存在。

### 上下文和输出限制

每次请求模型前统一由 `ContextBuilder` 构造上下文。它始终保留 system prompt 和当前用户消息，再从最新到最旧加入能放入预算的完整消息组。包含 `tool_calls` 的 assistant 消息及对应 tool messages 只能整组保留或整组省略；孤立或不完整工具消息不会发送给模型。发生裁剪时插入固定的“较早历史已省略”系统标记，不包含被省略内容、审批详情或其他敏感值，本轮不调用额外模型摘要。

默认限制如下：

- `MAX_CONTEXT_CHARS=120000`：发送给模型的完整消息 JSON 字符预算。
- `MAX_TOOL_RESULT_CHARS=20000`：任意工具结果或错误的 JSON 字符上限；截断结果包含 `truncated`、`original_chars` 和字符串 `preview`。
- `MAX_COMMAND_OUTPUT_CHARS=20000`：命令 stdout/stderr 合计保留上限；两个管道会并发持续排空，超过部分不进入内存结果。
- `MAX_USER_MESSAGE_CHARS=20000`：单条用户消息上限，超过时 API 返回 `413 message_too_large`。
- `MAX_MESSAGE_CHARS=40000`：单条模型 assistant 文本保存上限。
- `SESSION_RUN_LEASE_SECONDS=120`：同会话 active run 租约时长，运行期间按三分之一周期续期。

## 运行

### 后端

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### 启动 business-backend

`business-backend` 负责注册、邮箱验证码、登录、JWT 和 Refresh Token。Electron 只会自动启动本地 Python sidecar，**不会自动启动 Spring 业务后台**。注册页面出现“无法连接业务后台”或旧版本的 `Failed to fetch` 时，先确认下面的服务正在监听 `http://127.0.0.1:8081`。

#### 推荐方式：Docker Compose

先安装 Docker Desktop，并确认 Docker Engine 已启动。在仓库根目录复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

生成一对 RSA 2048 位密钥到本地 `.secrets/`。私钥必须是 PKCS#8 PEM，公钥必须是 X.509 PEM；该目录已被 Git 忽略：

```powershell
New-Item -ItemType Directory -Force .secrets | Out-Null
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .secrets\jwt-private.pem
openssl pkey -in .secrets\jwt-private.pem -pubout -out .secrets\jwt-public.pem
```

编辑 `.env`，至少替换以下占位值：

- `BUSINESS_DATABASE_PASSWORD`：PostgreSQL 强密码。
- `JWT_PRIVATE_KEY_PEM`：Docker Compose 下保留模板值 `file:/run/secrets/jwt-private.pem`。
- `JWT_PUBLIC_KEY_PEM`：Docker Compose 下保留模板值 `file:/run/secrets/jwt-public.pem`。
- `EMAIL_VERIFICATION_CODE_SECRET`：与 JWT 密钥无关的至少 32 字符安全随机值。
- `MAIL_HOST`、`MAIL_PORT`、`MAIL_USERNAME`、`MAIL_PASSWORD`、`MAIL_FROM`：实际 SMTP 配置。
- 按邮件服务商要求设置 `MAIL_SMTP_AUTH` 和 `MAIL_STARTTLS_ENABLE`。

随机数生成器：（终端运行）
function New-RandomSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()

    try {
        $rng.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    }
    finally {
        $rng.Dispose()
    }
}

$dbPassword = New-RandomSecret
$apiToken = New-RandomSecret
$emailSecret = New-RandomSecret

$dbPassword
$apiToken
$emailSecret

Docker Compose 会把 `.secrets/jwt-private.pem` 和 `.secrets/jwt-public.pem` 只读挂载到容器的 `/run/secrets/`，Spring 再通过上面的 `file:` 值读取。不要把私钥正文写进 `.env`，也不要把 `.secrets/` 加入 Git：

```dotenv
JWT_PRIVATE_KEY_PEM=file:/run/secrets/jwt-private.pem
JWT_PUBLIC_KEY_PEM=file:/run/secrets/jwt-public.pem
```

不要提交 `.env`、私钥或 SMTP 密码。配置完成后，只启动 PostgreSQL 和 Spring 业务后台：

```powershell
docker compose up -d postgres business-backend
docker compose logs -f business-backend
```

日志出现 Spring 启动完成后，另开终端验证：

```powershell
Invoke-WebRequest http://127.0.0.1:8081/.well-known/jwks.json -UseBasicParsing
```

应返回 HTTP `200` 和只包含公钥参数的 JWKS。随后启动前端或桌面端：

```powershell
Set-Location frontend
npm run desktop:dev
```

停止业务后台和数据库：

```powershell
docker compose stop business-backend postgres
```

不要使用 `docker compose down -v` 作为普通停止命令；`-v` 会删除 PostgreSQL volume 和其中的用户、Refresh Session 数据。

#### Windows 本地 Maven 调试

本地运行需要 Java 21、可访问的 PostgreSQL、上述 RSA/验证码/SMTP 配置。先在当前 PowerShell 会话设置环境变量，例如：

```powershell
$env:BUSINESS_DATABASE_URL = 'jdbc:postgresql://127.0.0.1:5432/local_agent'
$env:BUSINESS_DATABASE_USER = 'local_agent'
$env:BUSINESS_DATABASE_PASSWORD = '<数据库密码>'
$env:JWT_PRIVATE_KEY_PEM = Get-Content -Raw '.secrets\jwt-private.pem'
$env:JWT_PUBLIC_KEY_PEM = Get-Content -Raw '.secrets\jwt-public.pem'
$env:EMAIL_VERIFICATION_CODE_SECRET = '<至少 32 字符的独立随机值>'
$env:MAIL_HOST = '<SMTP 主机>'
$env:MAIL_PORT = '587'
$env:MAIL_USERNAME = '<SMTP 用户名>'
$env:MAIL_PASSWORD = '<SMTP 密码>'
$env:MAIL_FROM = '<发件邮箱>'
$env:MAIL_SMTP_AUTH = 'true'
$env:MAIL_STARTTLS_ENABLE = 'true'
Set-Location business-backend
.\mvnw.cmd spring-boot:run
```

生产配置缺少 RSA 密钥、验证码 secret、发件地址或数据库连接时会 fail closed，这是预期行为。默认业务后台地址为 `http://127.0.0.1:8081`；若修改端口，需要同步设置 Electron 的 `BUSINESS_API_BASE_URL`、浏览器构建的 `VITE_BUSINESS_API_BASE_URL`、Python 的 `JWKS_URL` 和一致的 `JWT_ISSUER`。

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认访问 `http://127.0.0.1:8000`，可用 `VITE_API_BASE_URL` 修改。
浏览器开发模式会从本地后端自动取得短期启动进程对应的令牌。若后端不在回环地址或前端不在默认允许来源，必须同时显式配置 `API_AUTH_TOKEN` 和仅供开发使用的 `VITE_API_AUTH_TOKEN`；注意 Vite 变量会进入前端产物，不适合分发构建。

### 桌面端

```bash
cd frontend
npm run desktop:dev
```

这会同时启动 Vite 和 Electron；在开发模式下，Electron 还会尝试自动拉起后端。
如果要打包桌面程序：

```bash
cd frontend
npm run desktop:build
```

Windows 生产构建会先按 `backend/requirements-sidecar.lock` 和 `backend/sidecar.spec` 生成 PyInstaller 独立后端，再通过 `extraResources` 把完整 sidecar 目录放入安装包的 `resources/backend`。安装后的应用不依赖系统 Python、pip、预装依赖或手动运行 Uvicorn；后端源码、开发 `.env`、开发数据库和开发 workspace 不会作为运行依赖打入安装包。构建脚本会下载固定版本的 Electron、NSIS 和 7zip 工具，校验固定 SHA-256 后复用本地缓存，因此全新构建机首次构建需要网络。

生产模式每次启动先选择一个动态空闲端口，并只在 `127.0.0.1` 启动应用自己拥有的 sidecar。主进程同时检查公开 `/health` 的 `service`/`version`，并使用本次随机启动令牌检查受保护的 `/api/runtime`；端口上即使已有返回 200 的其他服务也不会被接受。packaged 模式始终由 `randomBytes` 生成新的令牌并忽略继承的 `API_AUTH_TOKEN`，环境变量覆盖只用于开发/测试路径。实际 API URL 通过受限 preload IPC 传给 renderer，Bearer 令牌只由 Electron 主进程的网络层附加，不暴露给普通页面代码。后端缺失、启动超时、认证失败或版本不匹配时显示带诊断代码的错误页，并提供“重试启动”和“退出应用”。

Electron 使用 fail-closed trusted-renderer 边界。每次 IPC 调用必须同时满足：调用来自 `sender.mainFrame`、`event.sender` 与预期窗口的 `webContents` 是同一对象、窗口和 webContents 未销毁，并且页面 URL 匹配该窗口的信任目标。生产主窗口只信任打包后的精确 `dist/index.html` file URL；诊断窗口只信任本次加载的精确 `diagnostic.html` URL（包含编码后的诊断查询）；开发主窗口只信任配置的 Vite dev server 精确 origin，不把 `localhost` 与 `127.0.0.1` 混同。

IPC 最小权限矩阵如下：

- 主工作台：`runtime:get`、`credentials:load`、`credentials:save`、`credentials:delete`。
- 诊断页：`backend:retry`、`app:quit`。

两个窗口使用不同 preload。诊断页没有凭据或 runtime bridge，主工作台没有诊断 bridge；subframe、其他 webContents、`data:`、`javascript:`、HTTP(S) 页面和其他 file 页面均无法通过 handler 授权。两个窗口都默认拒绝新窗口和 webview，并在 `will-navigate`/`will-redirect` 阶段阻止不匹配目标；CSP 不是 IPC sender 校验的替代品。

主页面 CSP 由主进程在精确 main-frame 响应上注入。生产策略不含 `unsafe-eval` 或 `unsafe-inline`，`connect-src` 只包含本次动态 `http://127.0.0.1:<port>`，并将 `object-src`、`frame-src`、`base-uri`、`form-action` 和 `worker-src` 设为 `none`。开发策略额外允许精确 Vite HTTP/WebSocket origin；仅开发样式因为 Vite 动态注入 CSS 使用 `style-src 'unsafe-inline'`。诊断页是无网络的单文件错误页，因内联静态 CSS 保留受限的 `style-src 'unsafe-inline'`，但脚本只允许自身文件，`connect-src` 和 frame/object/form/base 均禁用。

生产数据全部位于 Electron `app.getPath('userData')` 下：

- `workspace/`：默认工作区，前端显示的路径来自受认证的 `/api/runtime`，不再硬编码开发目录。
- `data/agent.sqlite3`：会话和审批数据库。
- `logs/backend.log`：后端轮转日志。
- `logs/sidecar.log`：Electron 主进程和 sidecar 输出的轮转日志。
- `credentials/api-key.bin`：仅在 Electron `safeStorage` 可用时写入的加密 API Key。

Electron 退出时只终止本次应用启动并持有句柄的 sidecar；Windows 使用进程树清理，动态端口上的其他进程不会被终止。启动验证失败会在进入诊断页前立即停止仍匹配本次 attempt 的 sidecar，并清空旧 runtime/base URL；连续重试先确认旧 owned child 已停止，清理失败会阻止新进程启动而不会丢失 ownership 引用。迟到的旧进程 exit 事件不能清除新 attempt。日志按大小轮转，并过滤 Bearer、Authorization 和常见 API Key 形式；不要把完整用户消息写入新增日志。

sidecar 还会在绑定端口前用 `OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)` 获取当前 Electron 父进程的 Windows HANDLE，并用 `WaitForSingleObject` 等待该进程对象。正常退出、崩溃或强制终止都会使该对象进入 signaled 状态，sidecar 随即停止 Uvicorn 并释放动态端口；这不是 PID 轮询，因此 PID 被系统复用后不会误认新进程。父 PID 无效、HANDLE 无法打开或等待失败时均 fail-closed，sidecar 不继续提供服务。主进程正常退出时的 `taskkill /T` 只接收本实例保存的 owned child PID，不按端口或进程名终止其他程序。

当前生产 sidecar、图标、安装包和生命周期只面向 Windows x64。应用内部身份继续固定为 `com.local.agent`，Electron 也显式沿用原 `本地智能体` userData 目录，以兼容已有数据库、工作区和加密凭据；对外产品名统一为 `MyAgent`。项目自有 ICO/PNG 会用于安装包、应用 executable、窗口和开始菜单快捷方式，不再使用默认 Electron 图标。macOS/Linux 没有对应 executable、签名或生命周期配置，不能视为受支持平台。

### Windows 发布和签名

Windows 构建有两个互斥模式：

- `WINDOWS_RELEASE_MODE=unsigned-development`（默认）：用于本地开发验收，保留图标和版本资源，但明确禁用 Authenticode 签名；产物清单会记录实际 `NotSigned` 或其他系统状态。
- `WINDOWS_RELEASE_MODE=signed-release`：正式发布模式。缺少证书配置、sidecar 签名失败、Electron/NSIS 签名失败，或任一最终产物的签名、发布者、RFC 3161 时间戳校验不通过时，构建立即失败，不能降级生成 unsigned release。

正式发布只能通过构建环境的秘密配置注入以下变量，不能写入 `.env`、仓库、构建日志或产物：

- `WINDOWS_SIGN_CERTIFICATE_PATH`：受信任代码签名 PFX 的绝对路径。
- `WINDOWS_SIGN_CERTIFICATE_PASSWORD`：PFX 密码。
- `WINDOWS_SIGN_TIMESTAMP_URL`：HTTPS RFC 3161 时间戳服务。
- `WINDOWS_SIGN_EXPECTED_PUBLISHER`：证书的精确 publisher/common name。
- `WINDOWS_SIGNTOOL_PATH`：可选的 Windows SDK `signtool.exe` 路径；未设置时从 PATH 或 Windows Kits 查找。

`backend/scripts/build_sidecar.ps1` 会在 sidecar 被复制到 Electron `resources` 前完成 SHA-256 Authenticode 签名。electron-builder 随后签名应用 executable 和最终 NSIS installer。构建结束会在 `.tmp/electron-release-p2c/release-manifest.json` 记录版本、Windows/x64、发布模式、三个最终 executable 的相对路径、大小、SHA-256、实际 Authenticode 状态、发布者、时间戳状态，以及 Python、PyInstaller、Node、npm、Electron、electron-builder 和 NSIS 版本。清单不包含证书密码、API Key 或 Bearer token。

当前仓库不提供也不生成临时自签名证书。没有可信证书时，只能将 unsigned development build 用于本地验收，并声明“签名流水线已就绪”；不得称该产物已签名或适合可信分发。

### Windows 安装、升级和回滚

NSIS 使用稳定应用身份，允许选择安装目录，创建开始菜单快捷方式但不创建桌面快捷方式。卸载删除程序文件、快捷方式和对应卸载注册项；`deleteAppDataOnUninstall=false`，因此不会删除 Electron `userData` 中的会话 SQLite、加密凭据、日志和默认 workspace。用户数据的删除必须是独立、明确的用户操作，不能作为静默卸载副作用。

发布验收使用 `npm run desktop:smoke` 在 `.tmp` 下创建唯一安装目录和两个唯一 userData，实际静默安装最终 NSIS，而不是运行 `win-unpacked`。测试覆盖安装后启动、renderer 初始化、登录前 runtime 双边界验证、动态端口、正常退出、强制终止 Electron 后 sidecar 自动退出、端口释放、卸载清理和 userData 保留；仅当已提供真实登录态时才继续创建 UUID 会话和执行 mock 对话，不伪造生产 JWT。注册安装探测通过 .NET Registry API 读取 HKCU/HKLM 的 32/64 位卸载项，不调用 `reg.exe` 模糊搜索。产品按精确 `DisplayName`（产品名加版本）匹配；目录优先按精确 `InstallLocation` 匹配，仅当该字段缺失时才以 `UninstallString`/`QuietUninstallString` 中 executable 的精确父目录回退。不存在项正常视为未安装，拒绝访问、非字符串字段、损坏命令或其他无法确认状态均 fail-closed。该实现同时由 Windows PowerShell 5.1 和 PowerShell 7 契约测试覆盖。若系统已存在同名注册安装，脚本会 fail-closed，避免覆盖真实安装。安装/卸载可能触发的 GUI 或系统权限确认必须由操作者正常批准。

每次 `desktop:smoke` 在任何前置检查之前删除上一份成功证据；`desktop:build` 生成新产物前也执行同样的失效操作。测试先校验 manifest 中 installer、Electron executable 和 sidecar 的当前 SHA-256，再校验安装后两个 executable 与 manifest 一致。只有安装、正常退出、强制退出、两次端口释放、sidecar 清理、卸载注册项/快捷方式/程序目录清理和 userData 保留全部通过，才用同目录临时文件原子生成 `.tmp/electron-release-p2c/windows-install-smoke.json`。证据包含 UTC 时间、唯一 `run_id`、版本、发布模式、installer/安装后 Electron/安装后 sidecar/release manifest 的 SHA-256；旧证据、失败流程遗留证据或与当前 manifest/产物哈希不一致的证据均不得作为当前成功结果。

升级前应退出应用，并备份完整 `userData/data/agent.sqlite3`；为获得一致快照，不要在 sidecar 运行时直接复制 WAL 中的数据库。升级只覆盖程序目录，保留 `userData`。回滚前同样先备份：旧版本可能不理解新版本新增的 SQLite schema 或事件，不能假设降级后仍可读；应优先恢复与旧版本匹配的备份，或通过兼容版本导出数据。

本阶段只定义安全更新策略，不实现自动下载或安装。未来更新源必须使用 TLS 和受控发布渠道，安装前同时验证 release manifest 的 SHA-256、有效且带时间戳的 Authenticode 签名以及精确 publisher；任一验证失败必须拒绝更新。不得接入未签名或仅依赖传输层、没有产物完整性校验的更新源。

### API Key 存储

浏览器模式下，设置面板输入的 API Key 默认只保存在当前页面内存。所有 localStorage 快照永久排除 `api_key`，启动时会重写旧快照并删除其中遗留的明文 Key。刷新或关闭页面后需要重新输入。

Electron 仅通过 `credentials:load`、`credentials:save` 和 `credentials:delete` 三个专用 IPC 调用系统 `safeStorage`；preload 不暴露任意文件读写或 shell 能力。设置 Key 后写入加密文件，清空输入会删除该文件。若系统加密不可用，Key 只保存在当前 Electron 进程内存并在界面明确提示，绝不降级为明文落盘；退出应用后需要重新输入。

API Key 只随模型请求发送，不写入会话数据库、localStorage 或应用日志。

### 运行时诊断 API

仅通过本地进程令牌认证的 `GET /api/runtime` 返回 `service`、`version`、规范化基础 workspace 路径、服务端是否允许命令执行，以及 SQLite 类型/就绪状态。它不返回启动令牌、环境变量、API Key、用户目录或数据库凭据。Electron 登录前启动身份检查复用该端点；公开 `/health` 仅用于基础存活和服务版本识别。

## 环境变量

复制 `.env.example` 为 `.env` 后修改：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `WORKSPACE_DIR`
- `SQLITE_PATH`
- `ALLOW_COMMAND_EXECUTION`
- `MAX_AGENT_STEPS`
- `SESSION_RUN_LEASE_SECONDS`
- `MAX_CONTEXT_CHARS`
- `MAX_TOOL_RESULT_CHARS`
- `MAX_COMMAND_OUTPUT_CHARS`
- `MAX_USER_MESSAGE_CHARS`
- `MAX_MESSAGE_CHARS`
- `API_CORS_ORIGINS`：逗号分隔的允许来源；默认不含通配符。
- `API_AUTH_TOKEN`：可选固定启动令牌，至少 32 个字符；不配置时随机生成。
- `APPROVAL_TTL_SECONDS`：审批有效期，默认 900 秒。
- `ALLOW_NON_LOOPBACK_TOKEN_BOOTSTRAP`：默认关闭。仅用于经过容器 NAT、且宿主端口仍严格绑定回环地址的部署。

前端设置面板支持：

- 选择模型服务商预设：OpenAI、深度求索、通义千问、硅基流动或自定义。
- 输入接口密钥。
- 修改 OpenAI 兼容接口地址。
- 修改模型名。
- 开关命令执行。

说明：前端输入的接口密钥只用于调用模型接口，不写入后端 SQLite 会话数据库。

### Docker 网络边界

Compose 中 Uvicorn 在容器内默认监听 `0.0.0.0`，这是 Docker 端口转发访问容器所必需的；宿主端口明确发布到 `127.0.0.1`，因此默认不对局域网开放。可用 `BACKEND_HOST` 调整容器内监听地址、用 `BACKEND_PORT` 调整宿主端口。Docker NAT 会隐藏浏览器的回环客户端地址，因此 Compose 显式设置 `ALLOW_NON_LOOPBACK_TOKEN_BOOTSTRAP=true`；令牌引导仍校验 CORS 来源，这个开关不得与非回环宿主端口发布一起使用。不要把端口映射改成 `0.0.0.0:8000:8000`，除非明确接受局域网可访问风险并已配置固定令牌、关闭该引导开关、设置严格 CORS 和外层网络防护。前端容器端口同样只发布到宿主回环地址。

## MVP 边界与第二阶段

本阶段已实现注册邮箱验证码投递；密码重置、SSO/企业身份源、MFA、账户恢复、跨设备 Agent 数据同步和旧本地数据导入 UI 仍未实现。登录只建立身份和当前设备的数据隔离，不能把另一设备的 sessions、approvals、collaborations、RAG 或工作区带到本机。

已知风险与兼容性限制：认证限流为单实例内存桶，多 Spring 实例部署需换成 Redis/网关级共享限流；Access Token 到期且业务后台离线时无法 refresh，Python 会 fail closed；旧 `owner_user_id=NULL` 数据保持隐藏，必须经过显式迁移工具和归属确认；Python Store 通过 JWT 依赖建立的请求级 Principal 上下文强制 owner 范围，owner 不接受请求参数，若未来在请求上下文之外新增 Store 调用必须显式传播该上下文；Electron `safeStorage` 与发布链路以 Windows 为主要验收平台；本次未实际执行需要安装/卸载和 GUI 权限的完整 NSIS `desktop:smoke`，仅运行了其 Node/PowerShell 契约测试与新的登录前 runtime 冒烟逻辑。

## 测试

```bash
cd backend
pytest
```

```bash
cd frontend
npm run build
```

## 如何新增工具

1. 在 `backend/app/tools/` 写工具函数和参数模型。
2. 在 `backend/app/agent/tool_registry.py` 注册工具。
3. 在 `backend/app/tests/` 增加测试。
4. 前端若需展示新字段，可在工具面板或消息渲染中扩展。

## 后续扩展

- PostgreSQL Store
- Playwright 浏览器自动化工具
- 多 Agent 协作编排器
## 注册邮箱验证码

新用户注册前必须调用 `POST /api/v1/auth/register/email-code`，请求体为 `{ "email": "user@example.com" }`。成功返回 HTTP 202：

```json
{ "expiresInSeconds": 600, "resendAfterSeconds": 60 }
```

随后调用 `POST /api/v1/auth/register`，提交 `email`、严格六位数字的 `verificationCode`、`password`、`displayName` 和 `deviceId`。验证码验证与消费、用户创建和 Refresh Session 创建在同一事务中完成；成功用户保存为 `email_verified=true`。已存在但历史上未验证的用户仍可登录。

验证码由 `SecureRandom` 生成，数据库只保存独立 secret、随机 salt 和邮箱共同计算的 HMAC-SHA-256。验证码默认 10 分钟有效、60 秒发送冷却、最多失败 5 次且只能消费一次。发送接口使用独立的 IP 与标准化邮箱限流桶；已注册和未注册邮箱执行相同的挑战写入与 SMTP 投递流程并返回相同响应，避免通过状态、SMTP 故障或明显的流程时延枚举账号。SMTP 失败统一返回 `503 email_delivery_failed`，并删除本次未投递成功的挑战，允许立即重试。

部署时必须提供独立的 `EMAIL_VERIFICATION_CODE_SECRET`（至少 32 字符，不能复用 JWT 密钥），并配置 `MAIL_HOST`、`MAIL_PORT`、`MAIL_USERNAME`、`MAIL_PASSWORD`、`MAIL_SMTP_AUTH`、`MAIL_STARTTLS_ENABLE` 和 `MAIL_FROM`。可选调整 `EMAIL_VERIFICATION_TTL`、`EMAIL_VERIFICATION_RESEND_COOLDOWN`、`EMAIL_VERIFICATION_MAX_ATTEMPTS`。仓库不包含真实 SMTP 凭据。
