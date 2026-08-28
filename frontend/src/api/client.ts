export interface AgentSettings {
  api_provider: string
  api_key: string
  model: string
  api_base_url: string
  allow_command_execution: boolean
  max_agent_steps: number
  use_streaming: boolean
}

export interface ToolCallRecord {
  name: string
  arguments: Record<string, unknown>
  status: 'ok' | 'error'
  result?: unknown
  error?: unknown
}

export interface MessageItem {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: unknown
  name?: string | null
  tool_call_id?: string | null
}

export interface ChatResponse {
  session_id: string
  answer: string
  tool_calls: ToolCallRecord[]
  messages: MessageItem[]
}

export interface SessionInfo {
  session_id: string
  display_title: string
  archived_at?: string | null
  created_at: string
  updated_at: string
  messages: MessageItem[]
  tool_calls: ToolCallRecord[]
}

export interface SessionSummary {
  session_id: string
  display_title: string
  archived_at?: string | null
  created_at: string
  updated_at: string
  message_count: number
  tool_call_count: number
}

export interface ApprovalRequest {
  id: string
  session_id: string
  tool_name: string
  arguments: Record<string, unknown>
  reason: string
  details: Record<string, unknown>
  status: 'pending' | 'approved' | 'rejected' | 'consumed'
  created_at: string
  updated_at: string
  expires_at: string
  consumed_at?: string | null
  last_resume_outcome?: 'cancelled' | null
  replacement_approval_id?: string | null
}

export interface ToolMarketItem {
  name: string
  description: string
  schema: Record<string, unknown>
  enabled: boolean
}

export interface RuntimeInfo {
  service: string
  version: string
  workspace: string
  command_execution_allowed: boolean
  database: { type: string; status: string }
}

export interface ChatRequest {
  session_id: string
  message: string
  settings: AgentSettings
}

export interface ChatStreamHandlers {
  onToolCall?: (record: ToolCallRecord) => void
  onDelta?: (content: string) => void
  onDone?: (response: ChatResponse) => void
  onError?: (error: StreamErrorPayload) => void
  onCancelled?: (event: StreamCancelledPayload) => void
}

export interface StreamErrorPayload {
  code: string
  message: string
  session_id?: string
}

export interface StreamCancelledPayload {
  code: 'cancelled'
  message: string
  session_id?: string
}

export interface CollaborationAgent {
  id: string
  name: string
  role: string
  prompt: string
  position: number
  is_coordinator: boolean
}

export interface CollaborationRun {
  id: string
  collaboration_id: string
  user_message: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  fencing_token: number
  terminal_event?: 'done' | 'error' | 'cancelled' | null
  created_at: string
  updated_at: string
}

export interface CollaborationEvent {
  collaboration_id: string
  sequence: number
  run_id: string
  event: string
  agent_id?: string | null
  message_id?: string | null
  round?: number | null
  data: Record<string, unknown>
  created_at: string
}

export interface CollaborationInfo {
  id: string
  session_id: string
  title: string
  rounds: number
  created_at: string
  updated_at: string
  agents: CollaborationAgent[]
  runs: CollaborationRun[]
  events: CollaborationEvent[]
}

export interface CollaborationSummary {
  id: string
  session_id: string
  title: string
  rounds: number
  agent_count: number
  active_run_id?: string | null
  created_at: string
  updated_at: string
}

export type CollaborationSseEventName =
  | 'run_started' | 'agent_status' | 'agent_delta' | 'agent_message'
  | 'agent_tool_call' | 'round_completed' | 'done' | 'error' | 'cancelled'

export interface CollaborationStreamHandlers {
  onEvent?: (event: CollaborationSseEventName, payload: Record<string, unknown>) => void
}

export class ChatStreamError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'ChatStreamError'
    this.code = code
  }
}

export class ChatStreamCancelled extends Error {
  constructor(message = '任务已取消。') {
    super(message)
    this.name = 'ChatStreamCancelled'
  }
}

export class ApiRequestError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiRequestError'
    this.code = code
    this.status = status
  }
}

const runtimeEnv = import.meta.env ?? {}
const webApiBaseUrl = runtimeEnv.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
let apiToken = runtimeEnv.VITE_API_AUTH_TOKEN ?? ''
let tokenRequest: Promise<string> | null = null
let desktopRuntimeRequest: Promise<{ apiBaseUrl: string; runtime: RuntimeInfo }> | null = null

function desktopApp() {
  return typeof window === 'undefined' ? undefined : window.desktopApp
}

async function getApiBaseUrl(): Promise<string> {
  const desktop = desktopApp()
  if (!desktop) return webApiBaseUrl
  if (!desktopRuntimeRequest) {
    desktopRuntimeRequest = desktop.getRuntimeConfig()
  }
  const apiBaseUrl = (await desktopRuntimeRequest).apiBaseUrl
  const parsed = new URL(apiBaseUrl)
  if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1') {
    throw new Error('Electron 返回了无效的本地 API 地址')
  }
  return apiBaseUrl.replace(/\/$/, '')
}

async function getApiToken(apiBaseUrl: string): Promise<string> {
  if (desktopApp()) return ''
  if (apiToken) {
    return apiToken
  }
  if (!tokenRequest) {
    tokenRequest = fetch(`${apiBaseUrl}/auth/token`)
      .then(async response => {
        if (!response.ok) {
          if (desktopApp()) {
            return ''
          }
          throw new Error(`无法获取本地 API 令牌: ${response.status}`)
        }
        const payload = (await response.json()) as { token?: unknown }
        if (typeof payload.token !== 'string' || !payload.token) {
          throw new Error('本地 API 未返回有效令牌')
        }
        apiToken = payload.token
        return apiToken
      })
      .catch(error => {
        tokenRequest = null
        throw error
      })
  }
  return tokenRequest
}

async function apiHeaders(init?: RequestInit): Promise<Headers> {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  const token = await getApiToken(await getApiBaseUrl())
  if (token) {
    headers.set('X-Local-Agent-Token', token)
  }
  if (!desktopApp()) {
    const userToken = accessToken()
    if (userToken) headers.set('Authorization', `Bearer ${userToken}`)
  }
  return headers
}

async function authenticatedFetch(path: string, init?: RequestInit, retried = false, epoch = authSessionEpoch()): Promise<Response> {
  const apiBaseUrl = await getApiBaseUrl()
  assertAuthEpoch(epoch)
  const headers = await apiHeaders(init)
  assertAuthEpoch(epoch)
  const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers })
  assertAuthEpoch(epoch)
  if (response.status === 401 && !retried) {
    await refreshAuth()
    assertAuthEpoch(epoch)
    return authenticatedFetch(path, init, true, epoch)
  }
  return response
}

function assertAuthEpoch(epoch: number) {
  if (authSessionEpoch() !== epoch) throw new ApiRequestError('auth_session_changed', '登录状态已变化，请重新操作。', 401)
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const epoch = authSessionEpoch()
  const response = await authenticatedFetch(path, init, false, epoch)
  if (!response.ok) {
    const body = await response.text()
    assertAuthEpoch(epoch)
    try {
      const payload = JSON.parse(body) as { detail?: { code?: unknown; message?: unknown } }
      const detail = payload.detail
      if (detail && typeof detail.code === 'string' && typeof detail.message === 'string') {
        throw new ApiRequestError(detail.code, detail.message, response.status)
      }
    } catch (error) {
      if (error instanceof ApiRequestError) throw error
    }
    throw new ApiRequestError(
      'request_failed',
      `请求失败（HTTP ${response.status}），请重试。`,
      response.status,
    )
  }
  const payload = (await response.json()) as T
  assertAuthEpoch(epoch)
  return payload
}

export function createSession(_sessionId?: string) {
  return requestJson<{ session_id: string }>('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function getRuntimeInfo() {
  return requestJson<RuntimeInfo>('/api/runtime')
}

export function listSessions(archived = false) {
  return requestJson<SessionSummary[]>(`/api/sessions?archived=${archived ? 'true' : 'false'}`)
}

export function getSession(sessionId: string) {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}`)
}

export function sendMessage(payload: ChatRequest) {
  return requestJson<ChatResponse>('/api/chat', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function renameSession(sessionId: string, displayTitle: string) {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ display_title: displayTitle }),
  })
}

export function archiveSession(sessionId: string) {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: 'POST',
  })
}

export function unarchiveSession(sessionId: string) {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/unarchive`, {
    method: 'POST',
  })
}

export async function sendMessageStream(
  payload: ChatRequest,
  handlers: ChatStreamHandlers = {},
  signal?: AbortSignal,
) {
  return sendEventStream('/api/chat/stream', payload, handlers, signal)
}

export async function resumeApprovalStream(
  approvalId: string,
  settings: AgentSettings,
  handlers: ChatStreamHandlers = {},
  signal?: AbortSignal,
) {
  return sendEventStream(
    `/api/approvals/${encodeURIComponent(approvalId)}/resume/stream`,
    { settings },
    handlers,
    signal,
  )
}

async function sendEventStream(
  path: string,
  payload: unknown,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
) {
  let response: Response
  try {
    response = await authenticatedFetch(path, {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (error) {
    if (signal?.aborted) {
      throw new ChatStreamCancelled()
    }
    throw error
  }
  if (!response.ok) {
    const body = await response.text()
    try {
      const parsed = JSON.parse(body) as { detail?: { code?: unknown; message?: unknown } }
      const detail = parsed.detail
      if (detail && typeof detail.code === 'string' && typeof detail.message === 'string') {
        throw new ChatStreamError(detail.code, detail.message)
      }
    } catch (error) {
      if (error instanceof ChatStreamError) throw error
    }
    throw new ChatStreamError('request_failed', `请求失败（HTTP ${response.status}），请重试。`)
  }
  if (!response.body) {
    throw new Error('流式响应不可用')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const finalResponse: { value?: ChatResponse } = {}
  let terminalEvent: 'done' | 'error' | 'cancelled' | null = null

  function setTerminal(event: 'done' | 'error' | 'cancelled') {
    if (terminalEvent) {
      throw new ChatStreamError('invalid_sse', '服务端返回了重复的终态事件，请重试。')
    }
    terminalEvent = event
  }

  function consumeBlock(block: string) {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim()
      }
      if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart())
      }
    }
    if (!dataLines.length) {
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(dataLines.join('\n')) as unknown
    } catch {
      throw new ChatStreamError('invalid_sse', '流式响应格式无效，请重试。')
    }
    if (event === 'tool_call') {
      handlers.onToolCall?.(parsed as ToolCallRecord)
      return
    }
    if (event === 'delta') {
      const delta = parsed as { content?: unknown }
      if (typeof delta.content === 'string') {
        handlers.onDelta?.(delta.content)
      }
      return
    }
    if (event === 'done') {
      setTerminal('done')
      finalResponse.value = parsed as ChatResponse
      handlers.onDone?.(finalResponse.value)
      return
    }
    if (event === 'error') {
      setTerminal('error')
      const payload = parsed as Partial<StreamErrorPayload>
      const code = typeof payload.code === 'string' ? payload.code : 'stream_failed'
      const message = typeof payload.message === 'string' ? payload.message : '流式任务失败，请重试。'
      const normalized = { code, message, session_id: payload.session_id }
      handlers.onError?.(normalized)
      throw new ChatStreamError(code, message)
    }
    if (event === 'cancelled') {
      setTerminal('cancelled')
      const payload = parsed as Partial<StreamCancelledPayload>
      const normalized: StreamCancelledPayload = {
        code: 'cancelled',
        message: typeof payload.message === 'string' ? payload.message : '任务已取消。',
        session_id: payload.session_id,
      }
      handlers.onCancelled?.(normalized)
      throw new ChatStreamCancelled(normalized.message)
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() ?? ''
      blocks.forEach(consumeBlock)
    }
    buffer += decoder.decode()
    if (buffer.trim()) {
      consumeBlock(buffer)
    }
  } catch (error) {
    if (signal?.aborted && !(error instanceof ChatStreamCancelled)) {
      throw new ChatStreamCancelled()
    }
    throw error
  } finally {
    if (terminalEvent !== 'done') {
      await reader.cancel().catch(() => undefined)
    }
    reader.releaseLock()
  }
  if (!finalResponse.value) {
    throw new ChatStreamError('unexpected_eof', '流式响应意外结束，请重试。')
  }
  return finalResponse.value
}

export function listApprovals(status?: ApprovalRequest['status']) {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return requestJson<ApprovalRequest[]>(`/api/approvals${query}`)
}

export function approveRequest(approvalId: string) {
  return requestJson<ApprovalRequest>(`/api/approvals/${encodeURIComponent(approvalId)}/approve`, {
    method: 'POST',
  })
}

export function rejectRequest(approvalId: string) {
  return requestJson<ApprovalRequest>(`/api/approvals/${encodeURIComponent(approvalId)}/reject`, {
    method: 'POST',
  })
}

export function listTools() {
  return requestJson<ToolMarketItem[]>('/api/tools')
}

export function createCollaboration(payload: {
  session_id: string
  title: string
  rounds: number
  agents: CollaborationAgent[]
}) {
  return requestJson<CollaborationInfo>('/api/collaborations', {
    method: 'POST', body: JSON.stringify(payload),
  })
}

export function listCollaborations(sessionId?: string) {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  return requestJson<CollaborationSummary[]>(`/api/collaborations${query}`)
}

export function getCollaboration(collaborationId: string) {
  return requestJson<CollaborationInfo>(`/api/collaborations/${encodeURIComponent(collaborationId)}`)
}

export async function runCollaborationStream(
  collaborationId: string,
  payload: { message: string; settings?: AgentSettings },
  handlers: CollaborationStreamHandlers = {},
  signal?: AbortSignal,
) {
  let response: Response
  try {
    response = await authenticatedFetch(`/api/collaborations/${encodeURIComponent(collaborationId)}/runs/stream`, {
      method: 'POST', headers: { Accept: 'text/event-stream' }, body: JSON.stringify(payload), signal,
    })
  } catch (error) {
    if (signal?.aborted) throw new ChatStreamCancelled('协作任务已取消。')
    throw error
  }
  if (!response.ok) {
    const body = await response.text()
    try {
      const detail = (JSON.parse(body) as { detail?: { code?: unknown; message?: unknown } }).detail
      if (detail && typeof detail.code === 'string' && typeof detail.message === 'string') {
        throw new ChatStreamError(detail.code, detail.message)
      }
    } catch (error) {
      if (error instanceof ChatStreamError) throw error
    }
    throw new ChatStreamError('request_failed', `请求失败（HTTP ${response.status}），请重试。`)
  }
  if (!response.body) throw new ChatStreamError('stream_unavailable', '协作流式响应不可用。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminal: 'done' | 'error' | 'cancelled' | null = null
  const valid = new Set<CollaborationSseEventName>([
    'run_started', 'agent_status', 'agent_delta', 'agent_message', 'agent_tool_call',
    'round_completed', 'done', 'error', 'cancelled',
  ])
  const consume = (block: string) => {
    let name = 'message'
    const lines: string[] = []
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('event:')) name = line.slice(6).trim()
      if (line.startsWith('data:')) lines.push(line.slice(5).trimStart())
    }
    if (!lines.length || !valid.has(name as CollaborationSseEventName)) return
    let payload: Record<string, unknown>
    try { payload = JSON.parse(lines.join('\n')) as Record<string, unknown> }
    catch { throw new ChatStreamError('invalid_sse', '协作流式响应格式无效。') }
    const event = name as CollaborationSseEventName
    if (event === 'done' || event === 'error' || event === 'cancelled') {
      if (terminal) throw new ChatStreamError('invalid_sse', '服务端返回了重复的协作终态。')
      terminal = event
    }
    handlers.onEvent?.(event, payload)
    if (event === 'error') throw new ChatStreamError(
      typeof payload.code === 'string' ? payload.code : 'collaboration_failed',
      typeof payload.message === 'string' ? payload.message : '协作任务失败。',
    )
    if (event === 'cancelled') throw new ChatStreamCancelled(
      typeof payload.message === 'string' ? payload.message : '协作任务已取消。',
    )
  }
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value, { stream: true })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() ?? ''
      blocks.forEach(consume)
    }
    buffer += decoder.decode()
    if (buffer.trim()) consume(buffer)
  } catch (error) {
    if (signal?.aborted && !(error instanceof ChatStreamCancelled)) {
      throw new ChatStreamCancelled('协作任务已取消。')
    }
    throw error
  } finally {
    if (terminal !== 'done') await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
  if (terminal !== 'done') throw new ChatStreamError('unexpected_eof', '协作流式响应意外结束。')
}
import { accessToken, refreshAuth } from '../stores/auth.js'
import { authSessionEpoch } from '../stores/authInvalidation.js'
