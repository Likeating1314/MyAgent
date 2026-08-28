import assert from 'node:assert/strict'
import test from 'node:test'

import type { AgentSettings } from '../src/api/client.ts'
import {
  chatStorageKey,
  loadChatSnapshot,
  serializeChatSnapshot,
} from '../src/stores/settingsPersistence.ts'

class MemoryStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

const settings: AgentSettings = {
  api_provider: 'openai',
  api_key: 'sk-never-persist',
  model: 'test-model',
  api_base_url: 'https://example.test/v1',
  allow_command_execution: false,
  max_agent_steps: 4,
  use_streaming: true,
}

test('localStorage serialization permanently excludes api_key', () => {
  const serialized = serializeChatSnapshot('session', settings)
  assert.equal(serialized.includes('sk-never-persist'), false)
  assert.equal(Object.hasOwn(JSON.parse(serialized).settings, 'api_key'), false)
})

test('loading a legacy plaintext api_key rewrites the snapshot without it', () => {
  const storage = new MemoryStorage()
  storage.setItem(chatStorageKey, JSON.stringify({ sessionId: 'legacy', settings }))
  const snapshot = loadChatSnapshot(storage)
  assert.equal(snapshot.sessionId, 'legacy')
  assert.equal(Object.hasOwn(snapshot.settings ?? {}, 'api_key'), false)
  const rewritten = storage.getItem(chatStorageKey) ?? ''
  assert.equal(rewritten.includes('sk-never-persist'), false)
  assert.equal(Object.hasOwn(JSON.parse(rewritten).settings, 'api_key'), false)
})
