# Agent 开发提示词

你是一名资深全栈工程师和 AI Agent 架构师。请帮助我从零开发一个可运行的 Agent 项目。

## 一、项目目标

我要开发一个本地可运行的 AI Agent 应用。这个 Agent 应该能够：

1. 接收用户输入的自然语言任务。
2. 调用大模型进行任务理解、规划和决策。
3. 根据模型决策调用后端工具，例如读取文件、写入文件、运行命令、搜索项目内容。
4. 将工具执行结果再次返回给模型，让模型继续判断下一步。
5. 在任务完成后给用户输出清晰总结。
6. 前端使用 Vue 实现。
7. 后端使用 Python 实现。

请你不仅写代码，还要在开发过程中清楚解释：

1. 为什么这样设计。
2. 每个模块负责什么。
3. 一次用户命令从前端到后端再到模型和工具调用的完整流程。
4. 项目结构中每个文件的用途。
5. 如何运行、测试和后续扩展。

## 二、技术栈要求

### 前端

使用：

- Vue 3
- TypeScript
- Vite
- Pinia 或 Vue 原生状态管理，优先保持简单
- Fetch API 或 Axios
- 基础 CSS 或 Tailwind CSS，若没有必要，不要引入过重 UI 框架

前端需要实现：

1. 聊天输入框。
2. 对话消息列表。
3. Agent 思考中 / 执行中状态展示。
4. 工具调用记录展示，例如：
   - 调用了哪个工具
   - 参数是什么
   - 执行结果摘要
5. 错误提示。
6. 基础设置区域，例如模型名、API 地址、是否允许执行命令。

### 后端

使用：

- Python 3.11+
- FastAPI
- Pydantic
- Uvicorn
- OpenAI Python SDK 或兼容 OpenAI API 的 SDK
- SQLite，可选，用于保存会话历史

后端需要实现：

1. REST API。
2. Agent 主循环。
3. LLM 调用封装。
4. 工具注册系统。
5. 工具执行器。
6. 权限控制。
7. 会话历史管理。
8. 日志记录。

## 三、核心架构

请按下面的逻辑设计系统：

```text
用户输入
  ↓
Vue 前端
  ↓
FastAPI 后端
  ↓
Agent Controller
  ↓
LLM Client
  ↓
模型返回：直接回答 / 工具调用 / 需要澄清
  ↓
Tool Executor
  ↓
工具执行结果
  ↓
再次交给 LLM
  ↓
循环直到完成
  ↓
返回最终答案给前端
```

Agent 的核心循环可以抽象为：

```python
while not done:
    response = llm.generate(messages, tools)

    if response.has_tool_call:
        result = tool_executor.run(response.tool_call)
        messages.append(tool_result)
        continue

    if response.final_answer:
        return response.final_answer
```

## 四、必须实现的基础工具

第一版 Agent 至少实现以下工具：

### 1. read_file

功能：读取指定路径文件内容。

要求：

- 只能读取项目工作目录内的文件。
- 返回文本内容。
- 文件过大时需要截断并提示。

### 2. write_file

功能：写入指定路径文件内容。

要求：

- 只能写入项目工作目录内的文件。
- 如果文件已存在，需要支持覆盖或拒绝覆盖。
- 写入前最好记录日志。

### 3. list_files

功能：列出目录下文件。

要求：

- 只能列出项目工作目录内的内容。
- 忽略 `.git`、`node_modules`、`.venv`、`__pycache__` 等目录。

### 4. search_text

功能：在项目目录内搜索文本。

要求：

- 优先使用 Python 实现，避免直接依赖系统命令。
- 支持关键词搜索。
- 返回文件路径、行号和匹配行。

### 5. run_command

功能：运行命令。

要求：

- 默认禁用危险命令。
- 支持白名单，例如：
  - `python`
  - `pytest`
  - `npm`
  - `pnpm`
  - `uvicorn`
- 支持超时。
- 返回 stdout、stderr、exit code。
- 对删除文件、格式化磁盘、修改 git 历史等危险命令必须拒绝。

## 五、建议项目结构

请使用类似下面的结构：

```text
agent-project/
  README.md
  .env.example
  .gitignore
  docker-compose.yml
  backend/
    pyproject.toml
    requirements.txt
    app/
      main.py
      config.py
      models/
        __init__.py
        schemas.py
      api/
        __init__.py
        routes.py
      agent/
        __init__.py
        controller.py
        prompts.py
        memory.py
        llm_client.py
        tool_registry.py
        tool_executor.py
      tools/
        __init__.py
        file_tools.py
        search_tools.py
        command_tools.py
      services/
        __init__.py
        session_store.py
        logger.py
      tests/
        test_tools.py
        test_agent_loop.py
  frontend/
    package.json
    vite.config.ts
    index.html
    src/
      main.ts
      App.vue
      api/
        client.ts
      stores/
        chat.ts
      components/
        ChatWindow.vue
        MessageList.vue
        MessageItem.vue
        Composer.vue
        ToolCallPanel.vue
        SettingsPanel.vue
      styles/
        main.css
```

## 六、每个后端模块的职责

请按下面方式实现或解释：

### backend/app/main.py

FastAPI 入口文件。

职责：

- 创建 FastAPI app。
- 注册 CORS。
- 注册 API 路由。
- 提供健康检查接口。

### backend/app/config.py

配置管理。

职责：

- 读取环境变量。
- 管理模型名、API key、工作目录、命令执行开关。
- 使用 Pydantic Settings 或简单配置类。

### backend/app/api/routes.py

API 路由。

建议接口：

```text
GET  /health
POST /api/chat
GET  /api/sessions/{session_id}
POST /api/sessions
```

`POST /api/chat` 请求体示例：

```json
{
  "session_id": "default",
  "message": "请帮我查看项目结构",
  "settings": {
    "model": "gpt-4.1-mini",
    "allow_command_execution": false
  }
}
```

响应体示例：

```json
{
  "session_id": "default",
  "answer": "我查看了项目结构...",
  "tool_calls": [
    {
      "name": "list_files",
      "arguments": {
        "path": "."
      },
      "result": "backend/, frontend/, README.md"
    }
  ]
}
```

### backend/app/agent/controller.py

Agent 核心控制器。

职责：

- 接收用户消息。
- 组装 system prompt。
- 加载会话历史。
- 调用 LLM。
- 判断是否有工具调用。
- 执行工具。
- 将工具结果加入消息历史。
- 控制最大循环次数，避免无限循环。
- 返回最终答案。

### backend/app/agent/prompts.py

存放系统提示词。

系统提示词要约束 Agent：

- 必须优先理解用户目标。
- 工具调用前要选择最合适的工具。
- 不确定时可以提问。
- 不允许访问工作目录外文件。
- 不允许执行危险命令。
- 修改文件后需要说明改了什么。

### backend/app/agent/llm_client.py

封装大模型调用。

职责：

- 管理 OpenAI 或兼容 API 的请求。
- 将本地工具定义转换成模型可理解的 tool schema。
- 解析模型返回。
- 支持后续替换模型供应商。

### backend/app/agent/tool_registry.py

工具注册中心。

职责：

- 定义工具名称、描述、参数 schema。
- 把工具函数注册到统一字典。
- 提供 `get_tool(name)` 和 `list_tool_schemas()`。

### backend/app/agent/tool_executor.py

工具执行器。

职责：

- 根据模型返回的工具名和参数调用实际工具。
- 做参数校验。
- 捕获异常。
- 返回结构化结果。

### backend/app/agent/memory.py

会话记忆。

职责：

- 保存当前 session 的 messages。
- 可以先用内存字典实现。
- 后续再接 SQLite。

### backend/app/tools/file_tools.py

文件读写相关工具。

### backend/app/tools/search_tools.py

文本搜索相关工具。

### backend/app/tools/command_tools.py

命令执行相关工具。

重点处理：

- 白名单
- 超时
- 工作目录限制
- 危险命令过滤

## 七、每个前端模块的职责

### frontend/src/App.vue

应用主入口。

职责：

- 组织整体布局。
- 左侧或顶部放设置区域。
- 主区域放聊天窗口。

### frontend/src/api/client.ts

封装后端请求。

职责：

- `sendMessage()`
- `createSession()`
- `getSession()`
- 统一处理错误。

### frontend/src/stores/chat.ts

聊天状态管理。

职责：

- 保存 messages。
- 保存当前 session_id。
- 保存 loading 状态。
- 保存工具调用记录。
- 调用 API 并更新 UI。

### frontend/src/components/ChatWindow.vue

聊天窗口容器。

### frontend/src/components/MessageList.vue

消息列表。

### frontend/src/components/MessageItem.vue

单条消息展示。

### frontend/src/components/Composer.vue

输入框和发送按钮。

### frontend/src/components/ToolCallPanel.vue

工具调用记录展示。

### frontend/src/components/SettingsPanel.vue

设置区域。

包括：

- 模型名输入
- API Base URL
- 是否允许命令执行
- 最大 Agent 循环次数

## 八、开发流程要求

请严格按下面阶段开发。

### 阶段 1：初始化项目

1. 创建 `backend/` 和 `frontend/`。
2. 初始化 FastAPI 后端。
3. 初始化 Vue + Vite 前端。
4. 创建 README 和 `.env.example`。
5. 确保前后端都能启动。

### 阶段 2：实现后端基础 API

1. 实现 `/health`。
2. 实现 `/api/chat` 的最小版本。
3. 先不接真实模型，使用 mock agent 返回固定文本。
4. 前端可以成功调用后端。

### 阶段 3：实现工具系统

1. 实现 `ToolRegistry`。
2. 实现 `ToolExecutor`。
3. 实现基础工具：
   - `read_file`
   - `write_file`
   - `list_files`
   - `search_text`
   - `run_command`
4. 为工具写单元测试。

### 阶段 4：接入 LLM

1. 实现 `LLMClient`。
2. 支持 OpenAI API key。
3. 将工具 schema 传给模型。
4. 解析 tool calls。
5. 支持模型返回最终回答。

### 阶段 5：实现 Agent 循环

1. 用户输入进入 controller。
2. controller 调用 LLM。
3. 如果模型请求工具调用，则执行工具。
4. 将工具结果加入 messages。
5. 再次调用模型。
6. 到达最终答案或最大循环次数后结束。

### 阶段 6：实现前端完整交互

1. 聊天消息展示。
2. loading 状态。
3. 错误状态。
4. 工具调用记录。
5. 设置项。
6. 简单但清晰的 UI。

### 阶段 7：安全与限制

1. 文件路径必须限制在 workspace 内。
2. 命令执行默认关闭。
3. 危险命令拒绝执行。
4. 设置命令超时。
5. 不把 API key 返回给前端。

### 阶段 8：测试和文档

1. 后端添加 pytest 测试。
2. 前端至少确保构建通过。
3. README 写清楚：
   - 如何安装
   - 如何配置环境变量
   - 如何启动后端
   - 如何启动前端
   - 如何使用 Agent
   - 如何添加新工具

## 九、一次命令的完整流程说明

请在 README 或文档中解释这个流程：

```text
1. 用户在 Vue 前端输入：
   “请帮我搜索项目里所有 TODO”

2. 前端调用：
   POST /api/chat

3. 后端收到请求：
   - 获取 session_id
   - 保存用户消息
   - 构造 messages
   - 加载工具 schema

4. Agent 调用 LLM：
   LLM 判断需要使用 search_text 工具。

5. Agent 执行工具：
   search_text({"query": "TODO", "path": "."})

6. 工具返回结果：
   包含文件路径、行号、匹配文本。

7. Agent 将工具结果追加到上下文，再次调用 LLM。

8. LLM 生成最终总结：
   “我找到了 3 个 TODO，分别在这些文件...”

9. 后端返回前端：
   - 最终回答
   - 工具调用记录

10. 前端展示：
   - Agent 回答
   - 工具调用详情
```

## 十、代码质量要求

请遵守：

1. 代码要清晰，不要过度抽象。
2. 每个模块职责单一。
3. 对外 API 使用 Pydantic schema。
4. 工具结果使用结构化 JSON。
5. 所有路径操作必须使用 `pathlib.Path`。
6. 命令执行必须使用 `subprocess.run` 或 `asyncio.create_subprocess_exec`，不要拼接 shell 字符串。
7. 不要把 API key 写进代码。
8. 对异常做友好处理。
9. 对 Agent 循环设置最大步数，例如 8 步。
10. README 必须完整。

## 十一、环境变量

`.env.example` 至少包含：

```text
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
WORKSPACE_DIR=./workspace
ALLOW_COMMAND_EXECUTION=false
MAX_AGENT_STEPS=8
```

## 十二、后续扩展方向

请在文档中说明后续可以扩展：

1. 流式输出。
2. 多会话持久化。
3. SQLite / PostgreSQL 存储。
4. RAG 知识库。
5. 浏览器自动化工具。
6. Git 工具。
7. 多 Agent 协作。
8. 用户审批系统。
9. 插件化工具市场。
10. 部署到 Docker。

## 十三、请你输出的内容

请按照以下顺序完成：

1. 先给出总体架构说明。
2. 给出完整项目结构。
3. 说明一次用户命令的执行流程。
4. 开始创建项目文件。
5. 实现后端。
6. 实现前端。
7. 添加测试。
8. 添加 README。
9. 运行后端测试。
10. 运行前端构建。
11. 最后总结：
    - 实现了哪些功能
    - 如何启动
    - 哪些地方可以继续增强

请直接开始开发，不要只停留在方案层面。除非有关键问题无法判断，否则请做出合理默认选择并继续推进。
