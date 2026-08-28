import type { CollaborationEvent } from '../api/client'

export interface CollaborationMessageView {
  messageId: string
  runId: string
  agentId: string
  agentName: string
  role: string
  round: number
  content: string
  persisted: boolean
}

export function appendAgentDelta(
  messages: CollaborationMessageView[],
  payload: Record<string, unknown>,
): CollaborationMessageView[] {
  const messageId = typeof payload.message_id === 'string' ? payload.message_id : ''
  const content = typeof payload.content === 'string' ? payload.content : ''
  if (!messageId || !content) return messages
  const index = messages.findIndex(item => item.messageId === messageId)
  if (index < 0) {
    return [...messages, {
      messageId,
      runId: String(payload.run_id ?? ''),
      agentId: String(payload.agent_id ?? ''),
      agentName: String(payload.agent_name ?? 'Agent'),
      role: String(payload.role ?? ''),
      round: Number(payload.round ?? 0),
      content,
      persisted: false,
    }]
  }
  const copy = [...messages]
  copy[index] = { ...copy[index], content: copy[index].content + content }
  return copy
}

export function applyPersistedAgentMessage(
  messages: CollaborationMessageView[],
  payload: Record<string, unknown>,
): CollaborationMessageView[] {
  const messageId = String(payload.message_id ?? '')
  if (!messageId) return messages
  const next: CollaborationMessageView = {
    messageId,
    runId: String(payload.run_id ?? ''),
    agentId: String(payload.agent_id ?? ''),
    agentName: String(payload.agent_name ?? 'Agent'),
    role: String(payload.role ?? ''),
    round: Number(payload.round ?? 0),
    content: String(payload.content ?? ''),
    persisted: true,
  }
  const index = messages.findIndex(item => item.messageId === messageId)
  if (index < 0) return [...messages, next]
  const copy = [...messages]
  copy[index] = next
  return copy
}

export function messagesFromSnapshot(events: CollaborationEvent[]): CollaborationMessageView[] {
  return events
    .filter(event => event.event === 'agent_message')
    .map(event => ({
      messageId: String(event.message_id ?? event.data.message_id ?? ''),
      runId: event.run_id,
      agentId: String(event.agent_id ?? event.data.agent_id ?? ''),
      agentName: String(event.data.agent_name ?? 'Agent'),
      role: String(event.data.role ?? ''),
      round: Number(event.round ?? event.data.round ?? 0),
      content: String(event.data.content ?? ''),
      persisted: true,
    }))
}

export function recoverCollaborationMessages(events: CollaborationEvent[]) {
  return messagesFromSnapshot(events)
}

export class CollaborationTerminalGuard {
  private terminal: string | null = null

  accept(event: string) {
    if (!['done', 'error', 'cancelled'].includes(event)) return
    if (this.terminal) throw new Error('duplicate_terminal')
    this.terminal = event
  }
}
