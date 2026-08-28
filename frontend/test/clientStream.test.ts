import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ChatStreamCancelled,
  ChatStreamError,
  ApiRequestError,
  approveRequest,
  listSessions,
  resumeApprovalStream,
  sendMessageStream,
  type AgentSettings,
  type ChatResponse,
} from '../src/api/client.ts'

const settings: AgentSettings = {
  api_provider: 'openai',
  api_key: '',
  model: 'test-model',
  api_base_url: 'https://example.test/v1',
  allow_command_execution: false,
  max_agent_steps: 2,
  use_streaming: true,
}

const response: ChatResponse = {
  session_id: 'test-session',
  answer: 'hello',
  tool_calls: [],
  messages: [
    { role: 'user', content: 'run' },
    { role: 'assistant', content: 'hello' },
  ],
}

async function withSseResponse<T>(body: string, run: () => Promise<T>): Promise<T> {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/auth/token')) {
      return Response.json({ token: 'test-token' })
    }
    return new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }
  try {
    return await run()
  } finally {
    globalThis.fetch = originalFetch
  }
}

function payload() {
  return { session_id: 'test-session', message: 'run', settings }
}

test('client accepts delta plus done and ignores unknown events', async () => {
  const deltas: string[] = []
  const body = [
    'event: future_event\ndata: {"value":1}\n\n',
    'event: delta\ndata: {"content":"hello"}\n\n',
    `event: done\ndata: ${JSON.stringify(response)}\n\n`,
  ].join('')

  const result = await withSseResponse(body, () =>
    sendMessageStream(payload(), { onDelta: content => deltas.push(content) }),
  )

  assert.deepEqual(deltas, ['hello'])
  assert.deepEqual(result, response)
})

test('client surfaces a structured server error without accepting done', async () => {
  const body = 'event: error\ndata: {"code":"model_error","message":"模型服务暂时不可用"}\n\n'

  await withSseResponse(body, async () => {
    await assert.rejects(
      sendMessageStream(payload()),
      error => error instanceof ChatStreamError && error.code === 'model_error',
    )
  })
})

test('client surfaces server cancellation separately from errors', async () => {
  const body = 'event: cancelled\ndata: {"code":"cancelled","message":"任务已取消。"}\n\n'

  await withSseResponse(body, async () => {
    await assert.rejects(sendMessageStream(payload()), error => error instanceof ChatStreamCancelled)
  })
})

test('client reports malformed JSON as a recoverable protocol error', async () => {
  const body = 'event: delta\ndata: {not-json}\n\n'

  await withSseResponse(body, async () => {
    await assert.rejects(
      sendMessageStream(payload()),
      error => error instanceof ChatStreamError && error.code === 'invalid_sse',
    )
  })
})

test('client reports an unexpected EOF when no terminal event arrives', async () => {
  const body = 'event: delta\ndata: {"content":"partial"}\n\n'

  await withSseResponse(body, async () => {
    await assert.rejects(
      sendMessageStream(payload()),
      error => error instanceof ChatStreamError && error.code === 'unexpected_eof',
    )
  })
})

test('approval resume sends only settings and supports the existing SSE protocol', async () => {
  const originalFetch = globalThis.fetch
  let capturedBody = ''
  let capturedUrl = ''
  globalThis.fetch = async (input, init) => {
    const url = String(input)
    if (url.endsWith('/auth/token')) return Response.json({ token: 'test-token' })
    capturedUrl = url
    capturedBody = String(init?.body ?? '')
    return new Response(`event: done\ndata: ${JSON.stringify(response)}\n\n`, {
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }
  try {
    const result = await resumeApprovalStream('approval/id', settings)
    assert.equal(capturedUrl.endsWith('/api/approvals/approval%2Fid/resume/stream'), true)
    assert.deepEqual(JSON.parse(capturedBody), { settings })
    assert.equal('session_id' in JSON.parse(capturedBody), false)
    assert.deepEqual(result, response)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('approval resume abort is reported as cancellation', async () => {
  const originalFetch = globalThis.fetch
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
  const controller = new AbortController()
  const pending = resumeApprovalStream('approval-1', settings, {}, controller.signal)
  controller.abort()
  try {
    await assert.rejects(pending, error => error instanceof ChatStreamCancelled)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('approval API fallback never exposes raw JSON detail to the UI', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async input => {
    if (String(input).endsWith('/auth/token')) return Response.json({ token: 'test-token' })
    return new Response('{"detail":"raw backend payload"}', { status: 409 })
  }
  try {
    await assert.rejects(
      approveRequest('approval-1'),
      error =>
        error instanceof ApiRequestError &&
        error.code === 'request_failed' &&
        !error.message.includes('raw backend payload') &&
        !error.message.includes('{'),
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('401 performs exactly one refresh and one retry without looping', async () => {
  const originalFetch = globalThis.fetch
  let apiCalls = 0
  let refreshCalls = 0
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/auth/token')) return Response.json({ token: 'test-local-token' })
    if (url.endsWith('/api/v1/auth/refresh')) {
      refreshCalls += 1
      return Response.json({
        accessToken: 'new.access.jwt', expiresAt: '2030-01-01T00:00:00Z',
        user: { id: 'u', email: 'u@example.com', displayName: 'U', status: 'ACTIVE', emailVerified: false, roles: ['USER'] },
      })
    }
    if (url.includes('/api/sessions')) {
      apiCalls += 1
      return new Response('{"detail":{"code":"unauthorized","message":"认证失败"}}', { status: 401 })
    }
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await assert.rejects(listSessions(), error => error instanceof ApiRequestError && error.status === 401)
    assert.equal(refreshCalls, 1)
    assert.equal(apiCalls, 2)
  } finally {
    globalThis.fetch = originalFetch
  }
})
