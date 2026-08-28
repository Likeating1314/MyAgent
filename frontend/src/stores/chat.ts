import { reactive } from 'vue'
import {
  approveRequest,
  archiveSession as archiveSessionRequest,
  createSession,
  getSession,
  getRuntimeInfo,
  listApprovals,
  listSessions,
  listTools,
  rejectRequest,
  renameSession as renameSessionRequest,
  resumeApprovalStream,
  sendMessageStream,
  unarchiveSession as unarchiveSessionRequest,
  ChatStreamCancelled,
  type AgentSettings,
  type ApprovalRequest,
  type MessageItem,
  type RuntimeInfo,
  type SessionSummary,
  type ToolCallRecord,
  type ToolMarketItem,
} from '../api/client'
import { RequestLifecycle } from './requestLifecycle'
import { recoverStreamMessages } from './streamRecovery'
import {
  chooseSessionAfterArchive,
  approvalResultNotice,
  resolveApprovalState,
  runOperationOnce,
  type ApprovalOperation,
  type SessionOperation,
} from './sessionLifecycle'
import { loadChatSnapshot, saveChatSnapshot } from './settingsPersistence'
import { authSessionEpoch, registerAuthInvalidationHandler } from './authInvalidation.js'

const defaultSettings: AgentSettings = {
  api_provider: 'openai',
  api_key: '',
  model: 'gpt-4.1-mini',
  api_base_url: 'https://api.openai.com/v1',
  allow_command_execution: false,
  max_agent_steps: 8,
  use_streaming: true,
}

function normalizeSettings(settings?: Partial<AgentSettings>): AgentSettings {
  return { ...defaultSettings, ...(settings ?? {}), api_key: '', use_streaming: true }
}

const snapshot = loadChatSnapshot(localStorage)
const requestLifecycle = new RequestLifecycle()
let credentialWrite = Promise.resolve()

export const chatState = reactive({
  sessionId: '',
  messages: [] as MessageItem[],
  toolCalls: [] as ToolCallRecord[],
  sessions: [] as SessionSummary[],
  archivedSessions: [] as SessionSummary[],
  approvals: [] as ApprovalRequest[],
  tools: [] as ToolMarketItem[],
  loading: false,
  activeApprovalId: '',
  sessionOperations: {} as Record<string, SessionOperation | undefined>,
  approvalOperations: {} as Record<string, ApprovalOperation | undefined>,
  sessionOperationErrors: {} as Record<string, string | undefined>,
  approvalOperationErrors: {} as Record<string, string | undefined>,
  error: '',
  notice: '',
  credentialNotice: '',
  runtime: null as RuntimeInfo | null,
  settings: normalizeSettings(snapshot.settings),
})

export async function initializeChat() {
  if (window.desktopApp) {
    const credential = await window.desktopApp.credentials.load()
    chatState.settings = { ...chatState.settings, api_key: credential.apiKey }
    chatState.credentialNotice = credential.warning
  }
  chatState.runtime = await getRuntimeInfo()
  const [activeSessions, archivedSessions] = await Promise.all([listSessions(), listSessions(true)])
  chatState.sessions = activeSessions
  chatState.archivedSessions = archivedSessions
  if (!activeSessions.some(session => session.session_id === chatState.sessionId)) {
    chatState.sessionId = chooseSessionAfterArchive(activeSessions).sessionId
  }
  if (!activeSessions.length) {
    chatState.sessionId = (await createSession()).session_id
  }
  saveChatSnapshot(localStorage, chatState.sessionId, chatState.settings)
  await refreshContext()
  try {
    const session = await getSession(chatState.sessionId)
    chatState.messages = session.messages
    chatState.toolCalls = session.tool_calls
  } catch {
    chatState.messages = []
    chatState.toolCalls = []
  }
}

export async function refreshContext() {
  const [sessions, archivedSessions, approvals, tools] = await Promise.all([
    listSessions(),
    listSessions(true),
    listApprovals(),
    listTools(),
  ])
  chatState.sessions = sessions
  chatState.archivedSessions = archivedSessions
  chatState.approvals = approvals
  chatState.tools = tools
}

export function updateSettings(patch: Partial<AgentSettings>) {
  const previousApiKey = chatState.settings.api_key
  chatState.settings = { ...chatState.settings, ...patch, use_streaming: true }
  saveChatSnapshot(localStorage, chatState.sessionId, chatState.settings)
  if (window.desktopApp && chatState.settings.api_key !== previousApiKey) {
    const apiKey = chatState.settings.api_key
    credentialWrite = credentialWrite
      .catch(() => undefined)
      .then(async () => {
        const result = apiKey
          ? await window.desktopApp?.credentials.save(apiKey)
          : await window.desktopApp?.credentials.delete()
        if (result) chatState.credentialNotice = result.warning
      })
      .catch(() => {
        chatState.credentialNotice = 'API Key 安全存储更新失败，本次仅保留在页面内存。'
      })
  }
}

export async function switchSession(sessionId: string) {
  if (chatState.loading) {
    return
  }
  chatState.sessionId = sessionId
  chatState.error = ''
  saveChatSnapshot(localStorage, chatState.sessionId, chatState.settings)
  const session = await getSession(sessionId)
  chatState.messages = session.messages
  chatState.toolCalls = session.tool_calls
  await refreshContext()
}

export async function startNewSession() {
  if (chatState.loading) {
    return
  }
  const sessionId = (await createSession()).session_id
  await switchSession(sessionId)
}

export async function approveCommand(approvalId: string) {
  if (chatState.loading) return
  const approval = chatState.approvals.find(item => item.id === approvalId)
  if (!approval) throw new Error('审批记录不存在，请刷新后重试。')
  if (new Date(approval.expires_at).getTime() <= Date.now()) {
    throw new Error('审批已过期，不能继续执行。')
  }

  chatState.loading = true
  chatState.activeApprovalId = approvalId
  chatState.error = ''
  chatState.notice = '正在执行已批准的工具并继续原任务…'
  const controller = requestLifecycle.start()
  const authEpoch = authSessionEpoch()
  let fallbackMessages = [...chatState.messages]
  try {
    if (approval.session_id !== chatState.sessionId) {
      const session = await getSession(approval.session_id)
      if (session.archived_at) throw new Error('归档会话不能续跑，请先恢复会话。')
      chatState.sessionId = approval.session_id
      chatState.messages = session.messages
      chatState.toolCalls = session.tool_calls
      fallbackMessages = [...session.messages]
    }
    if (approval.status === 'pending') {
      await approveRequest(approvalId)
    } else if (approval.status !== 'approved') {
      throw new Error('当前审批状态不能继续执行。')
    }
    const response = await submitApprovalStreaming(approvalId, controller.signal)
    chatState.sessionId = response.session_id
    await refreshApprovalFacts(response.session_id)
    const state = approvalDisplayStateById(approvalId)
    chatState.notice = approvalResultNotice(state, 'done')
    saveChatSnapshot(localStorage, chatState.sessionId, chatState.settings)
  } catch (error) {
    if (authSessionEpoch() !== authEpoch) return
    await restoreSessionAfterTerminal(fallbackMessages)
    await refreshContext().catch(() => undefined)
    const state = approvalDisplayStateById(approvalId)
    if (error instanceof ChatStreamCancelled || controller.signal.aborted) {
      chatState.notice = approvalResultNotice(state, 'cancelled')
    } else {
      chatState.error = error instanceof Error ? error.message : '批准并继续失败'
      chatState.notice = approvalResultNotice(state, 'error')
    }
  } finally {
    if (authSessionEpoch() === authEpoch) {
      requestLifecycle.finish(controller)
      chatState.activeApprovalId = ''
      chatState.loading = false
    }
  }
}

export async function rejectCommand(approvalId: string) {
  return runOperationOnce(
    chatState.approvalOperations,
    chatState.approvalOperationErrors,
    approvalId,
    'reject',
    async () => {
      await rejectRequest(approvalId)
      await refreshContext()
    },
    message => `${message} 请刷新审批状态后重试。`,
  )
}

export async function renameSession(sessionId: string, displayTitle: string) {
  return runOperationOnce(
    chatState.sessionOperations,
    chatState.sessionOperationErrors,
    sessionId,
    'rename',
    async () => {
      await renameSessionRequest(sessionId, displayTitle)
      await refreshContext()
    },
    message => `${message} 请修正标题后重试。`,
  )
}

export async function archiveSession(sessionId: string) {
  return runOperationOnce(
    chatState.sessionOperations,
    chatState.sessionOperationErrors,
    sessionId,
    'archive',
    async () => {
      await archiveSessionRequest(sessionId)
      await refreshContext()
      if (chatState.sessionId !== sessionId) return
      const fallback = chatState.sessions[0]
      if (fallback) {
        await switchSession(fallback.session_id)
        return
      }
      const created = await createSession()
      await switchSession(created.session_id)
    },
    message => `${message} 请处理正在运行的任务或审批后重试。`,
  )
}

export async function restoreArchivedSession(sessionId: string) {
  return runOperationOnce(
    chatState.sessionOperations,
    chatState.sessionOperationErrors,
    sessionId,
    'unarchive',
    async () => {
      await unarchiveSessionRequest(sessionId)
      await switchSession(sessionId)
    },
    message => `${message} 请重试恢复。`,
  )
}

export async function submitMessage(message: string) {
  const trimmed = message.trim()
  if (!trimmed || chatState.loading) {
    return
  }

  chatState.loading = true
  chatState.error = ''
  chatState.notice = ''
  chatState.messages = [...chatState.messages, { role: 'user', content: trimmed }]
  const optimisticMessages = [...chatState.messages]
  const controller = requestLifecycle.start()
  const authEpoch = authSessionEpoch()

  try {
    const payload = {
      session_id: chatState.sessionId,
      message: trimmed,
      settings: { ...chatState.settings, use_streaming: true },
    }
    const response = await submitStreamingMessage(payload, controller.signal)
    chatState.sessionId = response.session_id
    chatState.messages = response.messages
    chatState.toolCalls = response.tool_calls
    saveChatSnapshot(localStorage, chatState.sessionId, chatState.settings)
    await refreshContext()
  } catch (error) {
    if (authSessionEpoch() !== authEpoch) return
    await restoreSessionAfterTerminal(optimisticMessages)
    if (error instanceof ChatStreamCancelled || controller.signal.aborted) {
      chatState.notice = '任务已取消，已同步当前会话。'
    } else {
      chatState.error = error instanceof Error ? error.message : '发送失败'
    }
    await refreshContext().catch(() => undefined)
  } finally {
    if (authSessionEpoch() === authEpoch) {
      requestLifecycle.finish(controller)
      chatState.loading = false
    }
  }
}

export function stopCurrentRequest() {
  if (requestLifecycle.stop()) {
    chatState.notice = '正在停止当前任务…'
  }
}

export function clearUserData() {
  stopCurrentRequest()
  chatState.sessionId = ''
  chatState.messages = []
  chatState.toolCalls = []
  chatState.sessions = []
  chatState.archivedSessions = []
  chatState.approvals = []
  chatState.tools = []
  chatState.loading = false
  chatState.activeApprovalId = ''
  chatState.sessionOperations = {}
  chatState.approvalOperations = {}
  chatState.sessionOperationErrors = {}
  chatState.approvalOperationErrors = {}
  chatState.error = ''
  chatState.notice = ''
  saveChatSnapshot(localStorage, '', chatState.settings)
}

registerAuthInvalidationHandler(clearUserData)

async function restoreSessionAfterTerminal(fallbackMessages: MessageItem[]) {
  try {
    const session = await getSession(chatState.sessionId)
    chatState.messages = recoverStreamMessages(session.messages, fallbackMessages)
    chatState.toolCalls = session.tool_calls
  } catch {
    chatState.messages = recoverStreamMessages(undefined, fallbackMessages)
  }
}

async function refreshApprovalFacts(sessionId: string) {
  const [session] = await Promise.all([getSession(sessionId), refreshContext()])
  chatState.messages = session.messages
  chatState.toolCalls = session.tool_calls
}

async function submitStreamingMessage(
  payload: { session_id: string; message: string; settings: AgentSettings },
  signal: AbortSignal,
) {
  const assistantIndex = chatState.messages.length
  chatState.messages = [...chatState.messages, { role: 'assistant', content: '' }]

  return sendMessageStream(
    payload,
    {
      onToolCall(record) {
        chatState.toolCalls = [...chatState.toolCalls, record]
      },
      onDelta(content) {
        const messages = [...chatState.messages]
        const existing = messages[assistantIndex]
        messages[assistantIndex] = {
          role: 'assistant',
          content: `${typeof existing?.content === 'string' ? existing.content : ''}${content}`,
        }
        chatState.messages = messages
      },
    },
    signal,
  )
}

async function submitApprovalStreaming(approvalId: string, signal: AbortSignal) {
  const assistantIndex = chatState.messages.length
  chatState.messages = [...chatState.messages, { role: 'assistant', content: '' }]
  return resumeApprovalStream(
    approvalId,
    { ...chatState.settings, use_streaming: true },
    {
      onToolCall(record) {
        chatState.toolCalls = [...chatState.toolCalls, record]
      },
      onDelta(content) {
        const messages = [...chatState.messages]
        const existing = messages[assistantIndex]
        messages[assistantIndex] = {
          role: 'assistant',
          content: `${typeof existing?.content === 'string' ? existing.content : ''}${content}`,
        }
        chatState.messages = messages
      },
    },
    signal,
  )
}

export function approvalDisplayState(approval: ApprovalRequest) {
  return resolveApprovalState(approval, chatState.toolCalls, {
    activeApprovalId: chatState.activeApprovalId,
    loading: chatState.loading,
  })
}

function approvalDisplayStateById(approvalId: string) {
  const approval = chatState.approvals.find(item => item.id === approvalId)
  return approval ? resolveApprovalState(approval, chatState.toolCalls) : undefined
}
