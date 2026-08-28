import { reactive } from 'vue'
import {
  ChatStreamCancelled,
  createCollaboration,
  getCollaboration,
  listCollaborations,
  runCollaborationStream,
  type AgentSettings,
  type CollaborationAgent,
  type CollaborationInfo,
  type CollaborationSummary,
} from '../api/client'
import { chatState } from './chat'
import {
  appendAgentDelta,
  applyPersistedAgentMessage,
  messagesFromSnapshot,
  type CollaborationMessageView,
} from './collaborationStream'
import { selectRoomForSession } from './collaborationSession'
import { authSessionEpoch, registerAuthInvalidationHandler } from './authInvalidation.js'

let activeController: AbortController | null = null

export const collaborationState = reactive({
  rooms: [] as CollaborationSummary[],
  room: null as CollaborationInfo | null,
  messages: [] as CollaborationMessageView[],
  statuses: {} as Record<string, string>,
  loading: false,
  creating: false,
  error: '',
  notice: '',
  currentRound: 0,
})

export async function initializeCollaborations() {
  const sessionId = chatState.sessionId
  const rooms = await listCollaborations(sessionId)
  collaborationState.rooms = rooms
  const roomId = selectRoomForSession(collaborationState.room, rooms, sessionId)
  if (roomId) {
    await openCollaboration(roomId)
    return
  }
  collaborationState.room = null
  collaborationState.messages = []
  collaborationState.statuses = {}
  collaborationState.currentRound = 0
}

export async function openCollaboration(roomId: string) {
  if (collaborationState.loading) return
  const room = await getCollaboration(roomId)
  collaborationState.room = room
  collaborationState.messages = messagesFromSnapshot(room.events)
  collaborationState.statuses = {}
  collaborationState.currentRound = Math.max(0, ...room.events.map(event => event.round ?? 0))
}

export async function createCollaborationRoom(payload: {
  title: string
  agents: CollaborationAgent[]
}) {
  collaborationState.creating = true
  collaborationState.error = ''
  try {
    const room = await createCollaboration({
      session_id: chatState.sessionId,
      title: payload.title,
      rounds: 2,
      agents: payload.agents,
    })
    collaborationState.rooms = await listCollaborations(chatState.sessionId)
    collaborationState.room = room
    collaborationState.messages = []
  } finally {
    collaborationState.creating = false
  }
}

export async function submitCollaborationMessage(message: string) {
  const room = collaborationState.room
  const trimmed = message.trim()
  if (!room || !trimmed || collaborationState.loading) return
  collaborationState.loading = true
  collaborationState.error = ''
  collaborationState.notice = ''
  collaborationState.statuses = {}
  const controller = new AbortController()
  activeController = controller
  const authEpoch = authSessionEpoch()
  try {
    await runCollaborationStream(
      room.id,
      { message: trimmed, settings: safeCollaborationSettings(chatState.settings) },
      {
        onEvent(event, payload) {
          if (event === 'run_started' && collaborationState.room) {
            const nextSequence = Math.max(0, ...collaborationState.room.events.map(item => item.sequence)) + 1
            collaborationState.room.events.push({
              collaboration_id: String(payload.collaboration_id ?? collaborationState.room.id),
              sequence: nextSequence, run_id: String(payload.run_id ?? ''), event: 'user_message',
              data: { content: trimmed }, created_at: new Date().toISOString(),
            })
          } else if (event === 'agent_delta') {
            collaborationState.messages = appendAgentDelta(collaborationState.messages, payload)
          } else if (event === 'agent_message') {
            collaborationState.messages = applyPersistedAgentMessage(collaborationState.messages, payload)
            if (collaborationState.room) {
              const nextSequence = Math.max(0, ...collaborationState.room.events.map(item => item.sequence)) + 1
              collaborationState.room.events.push({
                collaboration_id: String(payload.collaboration_id ?? collaborationState.room.id),
                sequence: nextSequence,
                run_id: String(payload.run_id ?? ''), event: 'agent_message',
                agent_id: String(payload.agent_id ?? ''), message_id: String(payload.message_id ?? ''),
                round: Number(payload.round ?? 0), data: payload, created_at: new Date().toISOString(),
              })
            }
          } else if (event === 'agent_status') {
            collaborationState.statuses[String(payload.agent_id ?? '')] = String(payload.status ?? '')
          } else if (event === 'round_completed') {
            collaborationState.currentRound = Number(payload.round ?? 0)
          }
        },
      },
      controller.signal,
    )
    collaborationState.notice = '两轮协作已完成。'
  } catch (error) {
    if (authSessionEpoch() !== authEpoch) return
    collaborationState.notice = error instanceof ChatStreamCancelled ? '协作已停止，正在同步记录。' : ''
    if (!(error instanceof ChatStreamCancelled)) {
      collaborationState.error = error instanceof Error ? error.message : '协作任务失败'
    }
  } finally {
    if (activeController === controller) activeController = null
    if (authSessionEpoch() === authEpoch) {
      collaborationState.loading = false
      await recoverCollaboration().catch(() => undefined)
      collaborationState.rooms = await listCollaborations(chatState.sessionId).catch(() => collaborationState.rooms)
    }
  }
}

export function stopCollaborationRun() {
  if (!activeController) return false
  activeController.abort()
  collaborationState.notice = '正在停止当前协作 run…'
  return true
}

export function clearCollaborationData() {
  stopCollaborationRun()
  activeController = null
  collaborationState.rooms = []
  collaborationState.room = null
  collaborationState.messages = []
  collaborationState.statuses = {}
  collaborationState.loading = false
  collaborationState.creating = false
  collaborationState.error = ''
  collaborationState.notice = ''
  collaborationState.currentRound = 0
}

registerAuthInvalidationHandler(clearCollaborationData)

export async function recoverCollaboration() {
  if (!collaborationState.room) return
  const room = await getCollaboration(collaborationState.room.id)
  collaborationState.room = room
  collaborationState.messages = messagesFromSnapshot(room.events)
  collaborationState.statuses = {}
  collaborationState.currentRound = Math.max(0, ...room.events.map(event => event.round ?? 0))
}

function safeCollaborationSettings(settings: AgentSettings): AgentSettings {
  return { ...settings, allow_command_execution: false, use_streaming: true }
}
