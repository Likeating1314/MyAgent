import assert from 'node:assert/strict'
import test from 'node:test'

test('browser registration sends code through the dedicated endpoint and includes verificationCode on register', async () => {
  const originalFetch = globalThis.fetch
  const requests: Array<{ url: string; body: Record<string, unknown> }> = []
  globalThis.fetch = async (input, init) => {
    const url = String(input); const body = JSON.parse(String(init?.body ?? '{}'))
    requests.push({ url, body })
    if (url.endsWith('/register/email-code')) return Response.json({ expiresInSeconds: 600, resendAfterSeconds: 60 }, { status: 202 })
    if (url.endsWith('/register')) return Response.json({ accessToken: 'access.jwt', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'u', email: 'user@example.com', displayName: 'User', status: 'ACTIVE', emailVerified: true, roles: ['USER'] } })
    throw new Error(`unexpected URL ${url}`)
  }
  try {
    const { sendRegistrationEmailCode, register, authState } = await import('../src/stores/auth.ts')
    const result = await sendRegistrationEmailCode('user@example.com')
    assert.deepEqual(result, { expiresInSeconds: 600, resendAfterSeconds: 60 })
    assert.equal(authState.submitting, false)
    await register('user@example.com', '123456', 'correct-horse-battery', 'User')
    assert.match(requests[0].url, /\/api\/v1\/auth\/register\/email-code$/)
    assert.deepEqual(requests[0].body, { email: 'user@example.com' })
    assert.equal(requests[1].body.verificationCode, '123456')
  } finally { globalThis.fetch = originalFetch }
})

test('browser registration code network failure uses an actionable Chinese message', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch') }
  try {
    const { sendRegistrationEmailCode } = await import('../src/stores/auth.ts')
    await assert.rejects(
      sendRegistrationEmailCode('user@example.com'),
      error => error instanceof Error && error.message === '无法连接业务后台，请确认服务已启动后重试。' && !error.message.includes('Failed to fetch'),
    )
  } finally { globalThis.fetch = originalFetch }
})
