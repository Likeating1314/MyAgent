import assert from 'node:assert/strict'
import test from 'node:test'

import type { CollaborationInfo, CollaborationSummary } from '../src/api/client.ts'
import { selectRoomForSession } from '../src/stores/collaborationSession.ts'
import { readFileSync } from 'node:fs'

function room(id: string, sessionId: string): CollaborationInfo {
  return {
    id, session_id: sessionId, title: id, rounds: 2,
    created_at: '', updated_at: '', agents: [], runs: [], events: [],
  }
}

function summary(id: string, sessionId: string): CollaborationSummary {
  return {
    id, session_id: sessionId, title: id, rounds: 2, agent_count: 2,
    created_at: '', updated_at: '',
  }
}

test('session A room is not reused after switching to session B and reopening collaboration', () => {
  const roomA = room('room-a', 'session-a')
  const roomsB = [summary('room-b', 'session-b')]
  assert.equal(selectRoomForSession(roomA, roomsB, 'session-b'), 'room-b')
})

test('switching to a session without rooms clears the previous collaboration room', () => {
  assert.equal(selectRoomForSession(room('room-a', 'session-a'), [], 'session-b'), null)
})

test('current room is retained only while it belongs to the active session', () => {
  const current = room('room-a', 'session-a')
  assert.equal(
    selectRoomForSession(current, [summary('room-a', 'session-a')], 'session-a'),
    'room-a',
  )
})

test('collaboration controls enforce the 44px interaction target', () => {
  const css = readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')
  assert.match(css, /\.collaboration-create button,[\s\S]*?min-height:\s*44px/)
  assert.match(css, /\.collaboration-create input\s*\{[\s\S]*?min-height:\s*44px/)
  assert.match(css, /\.coordinator-choice\s*\{[\s\S]*?min-height:\s*44px/)
  assert.doesNotMatch(css, /\.agent-form-title button\s*\{[^}]*min-height:\s*(?:3[0-9]|4[0-3])px/)
})
