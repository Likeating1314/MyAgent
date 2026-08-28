import type { AgentSettings } from '../api/client'

export const chatStorageKey = 'local-agent-ui'

export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface ChatSnapshot {
  sessionId?: string
  settings?: Partial<AgentSettings>
}

function withoutApiKey(settings?: Partial<AgentSettings>): Partial<AgentSettings> | undefined {
  if (!settings || typeof settings !== 'object') return undefined
  const { api_key: _discarded, ...persistable } = settings
  return persistable
}

export function serializeChatSnapshot(sessionId: string, settings: AgentSettings): string {
  return JSON.stringify({ sessionId, settings: withoutApiKey(settings) })
}

export function loadChatSnapshot(storage: StorageLike): ChatSnapshot {
  const raw = storage.getItem(chatStorageKey)
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as ChatSnapshot
    const settings = withoutApiKey(parsed.settings)
    const sanitized = {
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : undefined,
      settings,
    }
    if (parsed.settings && Object.hasOwn(parsed.settings, 'api_key')) {
      storage.setItem(chatStorageKey, JSON.stringify(sanitized))
    }
    return sanitized
  } catch {
    storage.removeItem(chatStorageKey)
    return {}
  }
}

export function saveChatSnapshot(
  storage: StorageLike,
  sessionId: string,
  settings: AgentSettings,
) {
  storage.setItem(chatStorageKey, serializeChatSnapshot(sessionId, settings))
}
