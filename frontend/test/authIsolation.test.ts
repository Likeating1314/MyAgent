import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

test('failed 401 refresh logs out, clears user stores, and cannot leak into the next user', async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: new MemoryStorage(),
  })
  const originalFetch = globalThis.fetch
  const { authState, login } = await import('../src/stores/auth.ts')
  const { registerAuthInvalidationHandler } = await import('../src/stores/authInvalidation.ts')
  const { listSessions } = await import('../src/api/client.ts')
  const userData = {
    sessionId: '',
    sessions: [] as string[],
    messages: [] as string[],
    rooms: [] as string[],
    collaborationMessages: [] as string[],
  }
  const unregister = registerAuthInvalidationHandler(() => {
    userData.sessionId = ''
    userData.sessions = []
    userData.messages = []
    userData.rooms = []
    userData.collaborationMessages = []
  })

  let loginUser = 'a'
  let refreshCalls = 0
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/auth/token')) return Response.json({ token: 'local-agent-test-token' })
    if (url.endsWith('/api/v1/auth/login')) {
      const user = loginUser
      return Response.json({
        accessToken: `${user}.access.jwt`,
        expiresAt: '2030-01-01T00:00:00Z',
        user: { id: user, email: `${user}@example.com`, displayName: user.toUpperCase(), status: 'ACTIVE', emailVerified: false, roles: ['USER'] },
      })
    }
    if (url.endsWith('/api/v1/auth/refresh')) {
      refreshCalls += 1
      return new Response('{"message":"refresh expired"}', { status: 401 })
    }
    if (url.endsWith('/api/v1/auth/logout')) return new Response(null, { status: 204 })
    if (url.includes('/api/sessions')) {
      return new Response('{"detail":{"code":"unauthorized","message":"认证失败"}}', { status: 401 })
    }
    throw new Error(`unexpected URL ${url}`)
  }

  try {
    await login('a@example.com', 'password-for-user-a')
    userData.sessionId = 'session-a'
    userData.sessions = ['session-a']
    userData.messages = ['user-a-secret']
    userData.rooms = ['room-a']
    userData.collaborationMessages = ['user-a-collaboration']

    await assert.rejects(listSessions())
    assert.equal(refreshCalls, 1)
    assert.equal(authState.authenticated, false)
    assert.equal(userData.sessionId, '')
    assert.deepEqual(userData.sessions, [])
    assert.deepEqual(userData.messages, [])
    assert.deepEqual(userData.rooms, [])
    assert.deepEqual(userData.collaborationMessages, [])

    loginUser = 'b'
    await login('b@example.com', 'password-for-user-b')
    assert.equal(authState.user?.id, 'b')
    assert.deepEqual(userData.sessions, [])
    assert.deepEqual(userData.messages, [])
    assert.deepEqual(userData.rooms, [])
    assert.deepEqual(userData.collaborationMessages, [])

    const chatSource = readFileSync(new URL('../src/stores/chat.ts', import.meta.url), 'utf8')
    const collaborationSource = readFileSync(new URL('../src/stores/collaboration.ts', import.meta.url), 'utf8')
    assert.match(chatSource, /registerAuthInvalidationHandler\(clearUserData\)/)
    assert.match(collaborationSource, /registerAuthInvalidationHandler\(clearCollaborationData\)/)
  } finally {
    unregister()
    globalThis.fetch = originalFetch
    Reflect.deleteProperty(globalThis, 'localStorage')
  }
})

test('logout invalidates authentication and user stores before a hanging request completes', async () => {
  const originalFetch = globalThis.fetch
  const { authState, login, logout } = await import('../src/stores/auth.ts')
  const { registerAuthInvalidationHandler } = await import('../src/stores/authInvalidation.ts')
  let releaseLogout!: () => void
  const hangingLogout = new Promise<void>(resolve => { releaseLogout = resolve })
  let visibleUserData = ['user-a-secret']
  const unregister = registerAuthInvalidationHandler(() => { visibleUserData = [] })
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/api/v1/auth/login')) return Response.json({
      accessToken: 'a.access.jwt', expiresAt: '2030-01-01T00:00:00Z',
      user: { id: 'a', email: 'a@example.com', displayName: 'A', status: 'ACTIVE', emailVerified: false, roles: ['USER'] },
    })
    if (url.endsWith('/api/v1/auth/logout')) {
      await hangingLogout
      return new Response(null, { status: 204 })
    }
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await login('a@example.com', 'password-for-user-a')
    const pending = logout()
    assert.equal(authState.authenticated, false)
    assert.equal(authState.user, null)
    assert.deepEqual(visibleUserData, [])
    releaseLogout()
    await pending
  } finally {
    unregister()
    globalThis.fetch = originalFetch
  }
})

test('delayed ordinary API response is rejected after auth epoch changes', async () => {
  const originalFetch = globalThis.fetch
  const { login, clearAuth } = await import('../src/stores/auth.ts')
  const { listSessions, ApiRequestError } = await import('../src/api/client.ts')
  let releaseSessions!: () => void
  const delayedSessions = new Promise<void>(resolve => { releaseSessions = resolve })
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/auth/token')) return Response.json({ token: 'local-agent-test-token' })
    if (url.endsWith('/api/v1/auth/login')) return Response.json({
      accessToken: 'a.access.jwt', expiresAt: '2030-01-01T00:00:00Z',
      user: { id: 'a', email: 'a@example.com', displayName: 'A', status: 'ACTIVE', emailVerified: false, roles: ['USER'] },
    })
    if (url.includes('/api/sessions')) {
      await delayedSessions
      return Response.json([{ session_id: 'session-a', display_title: 'A', created_at: '', updated_at: '', message_count: 1, tool_call_count: 0 }])
    }
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await login('a@example.com', 'password-for-user-a')
    const pending = listSessions()
    await new Promise(resolve => setTimeout(resolve, 0))
    clearAuth()
    releaseSessions()
    await assert.rejects(
      pending,
      error => error instanceof ApiRequestError && error.code === 'auth_session_changed',
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('stale renderer refresh cannot overwrite a newer user login', async () => {
  const originalFetch = globalThis.fetch
  const { authState, accessToken, login, refreshAuth } = await import('../src/stores/auth.ts')
  let releaseRefresh!: () => void
  const refreshGate = new Promise<void>(resolve => { releaseRefresh = resolve })
  globalThis.fetch = async (input, init) => {
    const url = String(input)
    if (url.endsWith('/api/v1/auth/login')) {
      const email = JSON.parse(String(init?.body)).email as string
      const user = email.startsWith('a@') ? 'a' : 'b'
      return Response.json({ accessToken: `${user}.access.jwt`, expiresAt: '2030-01-01T00:00:00Z', user: { id: user, email, displayName: user.toUpperCase(), status: 'ACTIVE', emailVerified: false, roles: ['USER'] } })
    }
    if (url.endsWith('/api/v1/auth/refresh')) {
      await refreshGate
      return Response.json({ accessToken: 'a.late.jwt', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'a', email: 'a@example.com', displayName: 'A', status: 'ACTIVE', emailVerified: false, roles: ['USER'] } })
    }
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await login('a@example.com', 'password-for-user-a')
    const staleRefresh = refreshAuth()
    await new Promise(resolve => setTimeout(resolve, 0))
    await login('b@example.com', 'password-for-user-b')
    releaseRefresh()
    await assert.rejects(staleRefresh, /登录状态已变化/)
    assert.equal(authState.user?.id, 'b')
    assert.equal(accessToken(), 'b.access.jwt')
  } finally { globalThis.fetch = originalFetch }
})

test('refresh failure invalidates immediately before a hanging automatic logout', async () => {
  const originalFetch = globalThis.fetch
  const { authState, login, refreshAuth } = await import('../src/stores/auth.ts')
  let releaseLogout!: () => void
  const logoutGate = new Promise<void>(resolve => { releaseLogout = resolve })
  let logoutStarted!: () => void
  const started = new Promise<void>(resolve => { logoutStarted = resolve })
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/api/v1/auth/login')) return Response.json({ accessToken: 'a.access.jwt', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'a', email: 'a@example.com', displayName: 'A', status: 'ACTIVE', emailVerified: false, roles: ['USER'] } })
    if (url.endsWith('/api/v1/auth/refresh')) return new Response('{"message":"expired"}', { status: 401 })
    if (url.endsWith('/api/v1/auth/logout')) { logoutStarted(); await logoutGate; return new Response(null, { status: 204 }) }
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await login('a@example.com', 'password-for-user-a')
    const pending = refreshAuth()
    await started
    assert.equal(authState.authenticated, false)
    assert.equal(authState.user, null)
    releaseLogout()
    await assert.rejects(pending)
  } finally { globalThis.fetch = originalFetch }
})

test('delayed JSON body is rejected when auth epoch changes during parsing', async () => {
  const originalFetch = globalThis.fetch
  const { login, clearAuth } = await import('../src/stores/auth.ts')
  const { listSessions, ApiRequestError } = await import('../src/api/client.ts')
  let releaseBody!: () => void
  const bodyGate = new Promise<void>(resolve => { releaseBody = resolve })
  globalThis.fetch = async input => {
    const url = String(input)
    if (url.endsWith('/auth/token')) return Response.json({ token: 'local-agent-test-token' })
    if (url.endsWith('/api/v1/auth/login')) return Response.json({ accessToken: 'a.access.jwt', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'a', email: 'a@example.com', displayName: 'A', status: 'ACTIVE', emailVerified: false, roles: ['USER'] } })
    if (url.includes('/api/sessions')) return { ok: true, status: 200, json: async () => { await bodyGate; return [{ session_id: 'session-a' }] } } as Response
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    await login('a@example.com', 'password-for-user-a')
    const pending = listSessions()
    await new Promise(resolve => setTimeout(resolve, 0))
    clearAuth()
    releaseBody()
    await assert.rejects(pending, error => error instanceof ApiRequestError && error.code === 'auth_session_changed')
  } finally { globalThis.fetch = originalFetch }
})
