import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ChatStreamCancelled,
  ChatStreamError,
  runCollaborationStream,
  type CollaborationEvent,
} from '../src/api/client.ts'
import {
  appendAgentDelta,
  CollaborationTerminalGuard,
  recoverCollaborationMessages,
} from '../src/stores/collaborationStream.ts'

async function withSse(body: string, run: () => Promise<unknown>) {
  const original = globalThis.fetch
  globalThis.fetch = async input => {
    if (String(input).endsWith('/auth/token')) return Response.json({ token: 'test-token' })
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
  }
  try { return await run() } finally { globalThis.fetch = original }
}

test('parses every collaboration SSE event', async () => {
  const names = [
    'run_started', 'agent_status', 'agent_delta', 'agent_message', 'agent_tool_call',
    'round_completed', 'done',
  ] as const
  const received: string[] = []
  const body = names.map(name => `event: ${name}\ndata: {"collaboration_id":"c","run_id":"r"}\n\n`).join('')
  await withSse(body, () => runCollaborationStream('c', { message: 'hello' }, {
    onEvent: event => received.push(event),
  }))
  assert.deepEqual(received, names)
})

test('agent_delta aggregates by message_id', () => {
  let messages = appendAgentDelta([], {
    message_id: 'm1', run_id: 'r', agent_id: 'a', agent_name: 'A', round: 1, content: 'hel',
  })
  messages = appendAgentDelta(messages, { message_id: 'm1', content: 'lo' })
  assert.equal(messages[0]?.content, 'hello')
})

test('multiple agent deltas never cross streams', () => {
  let messages = appendAgentDelta([], { message_id: 'm1', agent_id: 'a1', content: 'A' })
  messages = appendAgentDelta(messages, { message_id: 'm2', agent_id: 'a2', content: 'B' })
  messages = appendAgentDelta(messages, { message_id: 'm1', content: '1' })
  assert.deepEqual(messages.map(item => [item.messageId, item.content]), [['m1', 'A1'], ['m2', 'B']])
})

test('duplicate terminal is rejected', async () => {
  const guard = new CollaborationTerminalGuard()
  guard.accept('done')
  assert.throws(() => guard.accept('cancelled'), /duplicate_terminal/)
  await withSse(
    'event: done\ndata: {}\n\nevent: done\ndata: {}\n\n',
    async () => assert.rejects(runCollaborationStream('c', { message: 'x' }), error => error instanceof ChatStreamError),
  )
})

test('unexpected EOF recovery discards temporary deltas and uses snapshot', async () => {
  await withSse(
    'event: agent_delta\ndata: {"message_id":"temporary","content":"partial"}\n\n',
    async () => assert.rejects(
      runCollaborationStream('c', { message: 'x' }),
      error => error instanceof ChatStreamError && error.code === 'unexpected_eof',
    ),
  )
  const events: CollaborationEvent[] = [{
    collaboration_id: 'c', sequence: 1, run_id: 'r', event: 'agent_message',
    agent_id: 'a', message_id: 'saved', round: 1,
    data: { agent_name: 'A', role: '分析', content: 'persisted' }, created_at: new Date().toISOString(),
  }]
  assert.deepEqual(recoverCollaborationMessages(events).map(item => item.messageId), ['saved'])
})

test('stopping collaboration only aborts its own signal', async () => {
  const collaborationAbort = new AbortController()
  const chatAbort = new AbortController()
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    if (String(input).endsWith('/auth/token')) return Response.json({ token: 'test-token' })
    return await new Promise<Response>((_resolve, reject) => {
      if (init?.signal?.aborted) {
        reject(new DOMException('aborted', 'AbortError'))
        return
      }
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })
  }
  const pending = runCollaborationStream('c', { message: 'x' }, {}, collaborationAbort.signal)
  collaborationAbort.abort()
  try {
    await assert.rejects(pending, error => error instanceof ChatStreamCancelled)
    assert.equal(chatAbort.signal.aborted, false)
  } finally {
    globalThis.fetch = original
  }
})
