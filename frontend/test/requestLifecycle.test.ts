import assert from 'node:assert/strict'
import test from 'node:test'

import { RequestLifecycle } from '../src/stores/requestLifecycle.ts'
import { recoverStreamMessages } from '../src/stores/streamRecovery.ts'

test('stopping aborts the active request and the next request gets a fresh controller', () => {
  const lifecycle = new RequestLifecycle()
  const first = lifecycle.start()
  assert.equal(lifecycle.active, true)
  assert.equal(lifecycle.stop(), true)
  assert.equal(first.signal.aborted, true)
  assert.equal(lifecycle.active, false)

  lifecycle.finish(first)
  const second = lifecycle.start()
  assert.notEqual(second, first)
  assert.equal(second.signal.aborted, false)
  assert.equal(lifecycle.active, true)
})

test('finishing an older request cannot clear a newer controller', () => {
  const lifecycle = new RequestLifecycle()
  const first = lifecycle.start()
  const second = lifecycle.start()
  lifecycle.finish(first)
  assert.equal(lifecycle.active, true)
  assert.equal(lifecycle.stop(), true)
  assert.equal(second.signal.aborted, true)
})

test('stream recovery removes an empty assistant without duplicating the user message', () => {
  const optimistic = [
    { role: 'assistant' as const, content: 'older reply' },
    { role: 'user' as const, content: 'current task' },
  ]
  const persisted = [...optimistic, { role: 'assistant' as const, content: '' }]

  assert.deepEqual(recoverStreamMessages(persisted, optimistic), optimistic)
  assert.deepEqual(recoverStreamMessages(undefined, optimistic), optimistic)
})
