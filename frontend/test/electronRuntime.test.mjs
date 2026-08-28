import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile, stat } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  BackendStartupError,
  BackendSupervisor,
  buildSidecarArgs,
  findAvailablePort,
  runBackendStartupAttempt,
  sanitizeLogLine,
  startupDiagnostic,
  stopOwnedProcessTree,
  verifyBackend,
} from '../electron/backend-runtime.mjs'
import { CredentialStore, registerCredentialIpc } from '../electron/credential-store.mjs'
import { AuthService, registerAuthIpc } from '../electron/auth-service.mjs'
import { createDiagnosticBridge, createMainBridge } from '../electron/preload-bridge.mjs'
import {
  buildMainPageCsp,
  createApiAuthToken,
  createMainPageUrlMatcher,
  createRendererAuthorizer,
  installMainPageCsp,
  IPC_ROLE,
  lockWindowNavigation,
  registerTrustedIpcHandler,
  TrustedRendererError,
} from '../electron/renderer-security.mjs'

test('MyAgent branding preserves the legacy packaged userData directory', async () => {
  const mainSource = await readFile(new URL('../electron/main.mjs', import.meta.url), 'utf8')
  assert.match(mainSource, /app\.setPath\('userData', path\.join\(app\.getPath\('appData'\), '本地智能体'\)\)/)
})

function safeStorageMock(available = true) {
  return {
    isEncryptionAvailable: () => available,
    encryptString: value => Buffer.from(`encrypted:${value}`, 'utf8'),
    decryptString: value => value.toString('utf8').replace(/^encrypted:/, ''),
  }
}

function rendererFixture(url) {
  const state = { url }
  const mainFrame = {}
  Object.defineProperty(mainFrame, 'url', { get: () => state.url })
  const webContents = {
    mainFrame,
    isDestroyed: () => false,
    getURL: () => state.url,
  }
  const window = {
    webContents,
    isDestroyed: () => false,
  }
  return {
    state,
    window,
    webContents,
    mainFrame,
    event: { sender: webContents, senderFrame: mainFrame },
  }
}

function packagedRendererBoundary() {
  const mainUrl = 'file:///application/dist/index.html'
  const diagnosticUrl = 'file:///application/electron/diagnostic.html?code=failed&message=retry'
  const main = rendererFixture(mainUrl)
  const diagnostic = rendererFixture(diagnosticUrl)
  const authorize = createRendererAuthorizer({
    isPackaged: true,
    devServerUrl: 'http://127.0.0.1:5173',
    getMainWindow: () => main.window,
    getDiagnosticWindow: () => diagnostic.window,
    getMainPageUrl: () => mainUrl,
    getDiagnosticPageUrl: () => diagnosticUrl,
  })
  return { authorize, main, diagnostic, mainUrl, diagnosticUrl }
}

test('dynamic port is loopback-bindable and runtime URL is passed through the narrow bridge', async () => {
  const port = await findAvailablePort()
  assert.ok(port > 0 && port <= 65_535)
  const calls = []
  const bridge = createMainBridge((channel, ...args) => {
    calls.push([channel, ...args])
    return { apiBaseUrl: `http://127.0.0.1:${port}` }
  }, 'win32')
  assert.equal((await bridge.getRuntimeConfig()).apiBaseUrl, `http://127.0.0.1:${port}`)
  assert.deepEqual(calls, [['runtime:get']])
  assert.equal('readFile' in bridge, false)
  assert.equal('shell' in bridge, false)
})

test('sidecar launch binds the dynamic port to the exact Electron parent pid', () => {
  assert.deepEqual(buildSidecarArgs(43123, 9876), [
    '--port', '43123', '--parent-pid', '9876',
  ])
  assert.throws(() => buildSidecarArgs(0, 9876), TypeError)
  assert.throws(() => buildSidecarArgs(43123, 0), TypeError)
})

test('healthy service with the wrong identity is rejected before authenticated runtime', async () => {
  const requests = []
  const fetchImpl = async url => {
    requests.push(String(url))
    return Response.json({ status: 'ok', service: 'other-service', version: '0.1.0' })
  }
  await assert.rejects(
    verifyBackend({ baseUrl: 'http://127.0.0.1:12345', token: 'secret', fetchImpl }),
    error => error instanceof BackendStartupError && error.code === 'backend_identity_mismatch',
  )
  assert.deepEqual(requests, ['http://127.0.0.1:12345/health'])
})

test('sidecar startup failure produces a safe actionable diagnostic model', () => {
  const diagnostic = startupDiagnostic(
    new BackendStartupError('backend_executable_missing', '安装包缺少后端 sidecar。请重新安装应用。'),
  )
  assert.deepEqual(diagnostic, {
    code: 'backend_executable_missing',
    message: '安装包缺少后端 sidecar。请重新安装应用。',
  })
})

test('backend identity requires an authenticated matching runtime response', async () => {
  const requests = []
  const fetchImpl = async (url, init = {}) => {
    requests.push([String(url), init.headers])
    if (String(url).endsWith('/health')) {
      return Response.json({ status: 'ok', service: 'local-ai-agent', version: '0.1.0' })
    }
    return Response.json({ service: 'local-ai-agent', version: '0.1.0', workspace: 'C:\\data', database: { status: 'ready' } })
  }
  const runtime = await verifyBackend({ baseUrl: 'http://127.0.0.1:23456', token: 'private-token', fetchImpl })
  assert.equal(runtime.workspace, 'C:\\data')
  assert.deepEqual(requests[1][1], { 'X-Local-Agent-Token': 'private-token' })
})

test('safeStorage encrypts, reloads and deletes the API key', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-credential-'))
  const filePath = path.join(directory, 'api-key.bin')
  const store = new CredentialStore({ safeStorage: safeStorageMock(), filePath })
  assert.equal((await store.save('sk-test-secret')).storage, 'encrypted')
  assert.notEqual((await readFile(filePath, 'utf8')), 'sk-test-secret')
  const reloaded = new CredentialStore({ safeStorage: safeStorageMock(), filePath })
  assert.equal((await reloaded.load()).apiKey, 'sk-test-secret')
  await reloaded.delete()
  await assert.rejects(stat(filePath), error => error.code === 'ENOENT')
})

test('unavailable safeStorage keeps the API key in memory and writes no plaintext file', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-credential-memory-'))
  const filePath = path.join(directory, 'api-key.bin')
  const store = new CredentialStore({ safeStorage: safeStorageMock(false), filePath })
  const result = await store.save('sk-memory-only')
  assert.equal(result.storage, 'memory')
  assert.match(result.warning, /仅保留/)
  assert.equal((await store.load()).apiKey, 'sk-memory-only')
  await assert.rejects(stat(filePath), error => error.code === 'ENOENT')
})

test('credential IPC validates argument shape and preload exposes no arbitrary capability', async () => {
  const handlers = new Map()
  registerCredentialIpc({ handle: (name, handler) => handlers.set(name, handler) }, {
    load: async () => ({ apiKey: '' }),
    save: async value => ({ apiKey: value }),
    delete: async () => ({ apiKey: '' }),
  }, () => undefined)
  assert.throws(() => handlers.get('credentials:load')({}, 'unexpected'), TypeError)
  assert.throws(() => handlers.get('credentials:save')({}), TypeError)
  assert.throws(() => handlers.get('credentials:save')({}, {}), TypeError)
  assert.throws(() => handlers.get('credentials:save')({}, 'value', 'extra'), TypeError)
  const bridge = createMainBridge(() => undefined, 'win32')
  assert.deepEqual(Object.keys(bridge.credentials).sort(), ['delete', 'load', 'save'])
  assert.deepEqual(Object.keys(bridge.auth).sort(), ['login', 'logout', 'me', 'refresh', 'register', 'restore', 'sendRegistrationEmailCode', 'state'])
  assert.equal('diagnostics' in bridge, false)
  const diagnosticBridge = createDiagnosticBridge(() => undefined, 'win32')
  assert.deepEqual(Object.keys(diagnosticBridge.diagnostics).sort(), ['quit', 'retry'])
  assert.equal('credentials' in diagnosticBridge, false)
  assert.equal('getRuntimeConfig' in diagnosticBridge, false)
})

test('trusted IPC rejects correct URLs from another webContents and from subframes', () => {
  const { authorize, main } = packagedRendererBoundary()
  const impostor = rendererFixture(main.state.url)
  assert.throws(() => authorize(impostor.event, 'runtime:get'), TrustedRendererError)

  const subframe = { url: main.state.url }
  assert.throws(
    () => authorize({ sender: main.webContents, senderFrame: subframe }, 'runtime:get'),
    TrustedRendererError,
  )
})

test('all IPC capabilities reject data, http and other file pages', () => {
  const boundary = packagedRendererBoundary()
  const badUrls = [
    'data:text/html,untrusted',
    'http://127.0.0.1:5173/',
    'file:///application/dist/other.html',
  ]
  for (const [capability, role] of Object.entries(IPC_ROLE)) {
    const fixture = role === 'main' ? boundary.main : boundary.diagnostic
    const originalUrl = fixture.state.url
    for (const url of badUrls) {
      fixture.state.url = url
      assert.throws(() => boundary.authorize(fixture.event, capability), TrustedRendererError)
    }
    fixture.state.url = originalUrl
  }
})

test('capability matrix prevents diagnostic credentials and main diagnostic commands', () => {
  const { authorize, main, diagnostic } = packagedRendererBoundary()
  for (const capability of ['runtime:get', 'credentials:load', 'credentials:save', 'credentials:delete', 'auth:state', 'auth:restore', 'auth:register', 'auth:send-registration-code', 'auth:login', 'auth:refresh', 'auth:logout', 'auth:me']) {
    assert.doesNotThrow(() => authorize(main.event, capability))
    assert.throws(() => authorize(diagnostic.event, capability), TrustedRendererError)
  }
  for (const capability of ['backend:retry', 'app:quit']) {
    assert.doesNotThrow(() => authorize(diagnostic.event, capability))
    assert.throws(() => authorize(main.event, capability), TrustedRendererError)
  }
})

test('auth refresh token uses a distinct safeStorage file and logout clears credentials', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-auth-'))
  const filePath = path.join(directory, 'auth', 'refresh-token.bin')
  const calls = []
  const fetchImpl = async (url, init = {}) => {
    calls.push([String(url), init])
    if (String(url).endsWith('/logout')) return new Response(null, { status: 204 })
    return Response.json({ accessToken: 'access.jwt', refreshToken: 'refresh-secret', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'u', email: 'u@example.com', displayName: 'U' } })
  }
  const auth = new AuthService({ safeStorage: safeStorageMock(), filePath, businessBaseUrl: 'http://127.0.0.1:8081', fetchImpl })
  assert.equal((await auth.login({ email: 'u@example.com', password: 'password-ten' })).authenticated, true)
  assert.notEqual((await readFile(filePath, 'utf8')), 'refresh-secret')
  assert.equal(calls[0][1].body.includes('password-ten'), true)
  await auth.logout()
  assert.equal(auth.accessToken, '')
  await assert.rejects(stat(filePath), error => error.code === 'ENOENT')
})

test('Electron registration code request is strict and never changes auth state or operation', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-auth-code-'))
  const filePath = path.join(directory, 'auth', 'refresh-token.bin')
  const calls = []
  const auth = new AuthService({ safeStorage: safeStorageMock(), filePath, businessBaseUrl: 'http://127.0.0.1:8081', fetchImpl: async (url, init) => { calls.push([url, init]); return Response.json({ expiresInSeconds: 600, resendAfterSeconds: 60 }, { status: 202 }) } })
  const before = auth.authOperation
  assert.deepEqual(await auth.sendRegistrationEmailCode({ email: 'user@example.com' }), { expiresInSeconds: 600, resendAfterSeconds: 60 })
  assert.equal(auth.authOperation, before)
  assert.equal(auth.state().authenticated, false)
  assert.match(String(calls[0][0]), /\/api\/v1\/auth\/register\/email-code$/)
  await assert.rejects(auth.sendRegistrationEmailCode({ email: 'bad', extra: true }), TypeError)
  await assert.rejects(auth.sendRegistrationEmailCode({ email: 'bad' }), TypeError)
  await assert.rejects(auth.register({ email: 'user@example.com', password: 'correct-horse-battery', displayName: 'User' }), TypeError)
  await assert.rejects(auth.register({ email: 'user@example.com', verificationCode: '12x456', password: 'correct-horse-battery', displayName: 'User' }), TypeError)
})

test('Electron registration code network failure does not expose raw fetch errors', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-auth-code-network-'))
  const auth = new AuthService({ safeStorage: safeStorageMock(), filePath: path.join(directory, 'refresh-token.bin'), businessBaseUrl: 'http://127.0.0.1:8081', fetchImpl: async () => { throw new TypeError('fetch failed') } })
  await assert.rejects(
    auth.sendRegistrationEmailCode({ email: 'user@example.com' }),
    error => error instanceof Error && error.message === '无法连接业务后台，请确认服务已启动后重试。' && !error.message.includes('fetch failed'),
  )
})

test('electron logout clears credentials before a hanging backend request completes', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'auth-hanging-logout-'))
  const filePath = path.join(root, 'refresh-token.bin')
  let release
  const hanging = new Promise(resolve => { release = resolve })
  const fetchImpl = async (url) => {
    if (String(url).endsWith('/logout')) {
      await hanging
      return new Response(null, { status: 204 })
    }
    return Response.json({ accessToken: 'access.jwt', refreshToken: 'refresh-secret', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'u' } })
  }
  const auth = new AuthService({ safeStorage: safeStorageMock(), filePath, businessBaseUrl: 'http://127.0.0.1:8081', fetchImpl })
  await auth.login({ email: 'u@example.com', password: 'correct-horse-battery' })
  const pending = auth.logout()
  assert.equal(auth.state().authenticated, false)
  assert.equal(existsSync(filePath), false)
  release()
  await pending
})

test('stale Electron refresh cannot overwrite newer access and refresh credentials', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'auth-stale-refresh-'))
  const filePath = path.join(root, 'refresh-token.bin')
  let releaseRefresh
  const refreshGate = new Promise(resolve => { releaseRefresh = resolve })
  const fetchImpl = async (url, init = {}) => {
    if (String(url).endsWith('/refresh')) {
      await refreshGate
      return Response.json({ accessToken: 'a.late.jwt', refreshToken: 'a-late-refresh', expiresAt: '2030-01-01T00:00:00Z', user: { id: 'a' } })
    }
    const email = JSON.parse(String(init.body)).email
    const user = email.startsWith('a@') ? 'a' : 'b'
    return Response.json({ accessToken: `${user}.access.jwt`, refreshToken: `${user}-refresh`, expiresAt: '2030-01-01T00:00:00Z', user: { id: user } })
  }
  const storage = safeStorageMock()
  const auth = new AuthService({ safeStorage: storage, filePath, businessBaseUrl: 'http://127.0.0.1:8081', fetchImpl })
  await auth.login({ email: 'a@example.com', password: 'password-for-user-a' })
  const staleRefresh = auth.refresh()
  await new Promise(resolve => setTimeout(resolve, 0))
  await auth.login({ email: 'b@example.com', password: 'password-for-user-b' })
  releaseRefresh()
  await assert.rejects(staleRefresh, /登录状态已变化/)
  assert.equal(auth.accessToken, 'b.access.jwt')
  assert.equal(storage.decryptString(await readFile(filePath)), 'b-refresh')
})

test('auth device id persists independently so refresh restore survives restart', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'agent-auth-device-'))
  const filePath = path.join(directory, 'auth', 'refresh-token.bin')
  const deviceIdFile = path.join(directory, 'auth', 'device-id')
  const first = new AuthService({ safeStorage: safeStorageMock(), filePath, deviceIdFile, businessBaseUrl: 'http://127.0.0.1:8081' })
  const second = new AuthService({ safeStorage: safeStorageMock(), filePath, deviceIdFile, businessBaseUrl: 'http://127.0.0.1:8081' })
  assert.equal(second.deviceId, first.deviceId)
  assert.match(await readFile(deviceIdFile, 'utf8'), /^[0-9a-f-]{36}$/i)
})

test('auth IPC validates sender and one-shot credential argument shape', () => {
  const handlers = new Map()
  const authorized = []
  registerAuthIpc({ handle: (name, handler) => handlers.set(name, handler) }, {
    state: () => ({}), restore: () => ({}), register: () => ({}), sendRegistrationEmailCode: () => ({}), login: () => ({}), refresh: () => ({}), logout: () => ({}), me: () => ({}),
  }, (_event, capability) => authorized.push(capability))
  assert.doesNotThrow(() => handlers.get('auth:login')({}, { email: 'bad' }))
  assert.throws(() => handlers.get('auth:logout')({}, 'extra'), TypeError)
  assert.throws(() => handlers.get('auth:send-registration-code')({}), TypeError)
  handlers.get('auth:state')({})
  assert.deepEqual(authorized, ['auth:login', 'auth:logout', 'auth:send-registration-code', 'auth:state'])
})

test('registered IPC handler checks sender before invoking capability code', () => {
  const handlers = new Map()
  const boundary = packagedRendererBoundary()
  let invoked = false
  registerTrustedIpcHandler(
    { handle: (name, handler) => handlers.set(name, handler) },
    'runtime:get',
    boundary.authorize,
    () => {
      invoked = true
    },
  )
  const impostor = rendererFixture(boundary.mainUrl)
  assert.throws(() => handlers.get('runtime:get')(impostor.event), TrustedRendererError)
  assert.equal(invoked, false)
})

test('dev renderer boundary allows only the configured Vite origin', () => {
  const main = rendererFixture('http://127.0.0.1:5173/settings')
  const authorize = createRendererAuthorizer({
    isPackaged: false,
    devServerUrl: 'http://127.0.0.1:5173',
    getMainWindow: () => main.window,
    getDiagnosticWindow: () => null,
    getMainPageUrl: () => '',
    getDiagnosticPageUrl: () => '',
  })
  assert.doesNotThrow(() => authorize(main.event, 'runtime:get'))
  for (const url of ['http://localhost:5173/', 'http://127.0.0.1:5174/', 'https://127.0.0.1:5173/']) {
    main.state.url = url
    assert.throws(() => authorize(main.event, 'runtime:get'), TrustedRendererError)
  }

  const allowsNavigation = createMainPageUrlMatcher({
    isPackaged: false,
    packagedPageUrl: '',
    devServerUrl: 'http://127.0.0.1:5173',
  })
  assert.equal(allowsNavigation('http://127.0.0.1:5173/other-route'), true)
  assert.equal(allowsNavigation('http://localhost:5173/'), false)
  assert.equal(allowsNavigation('data:text/html,bad'), false)
})

test('navigation guard blocks external pages, new windows and webviews', () => {
  const handlers = new Map()
  let openHandler
  const webContents = {
    on: (name, handler) => handlers.set(name, handler),
    setWindowOpenHandler: handler => { openHandler = handler },
  }
  const expected = 'file:///application/dist/index.html'
  lockWindowNavigation(webContents, url => url === expected)

  const allowed = { prevented: false, preventDefault() { this.prevented = true } }
  handlers.get('will-navigate')(allowed, expected)
  assert.equal(allowed.prevented, false)

  for (const url of ['data:text/html,bad', 'javascript:alert(1)', 'https://example.invalid/', 'file:///application/other.html']) {
    const event = { prevented: false, preventDefault() { this.prevented = true } }
    handlers.get('will-navigate')(event, url)
    assert.equal(event.prevented, true)
  }
  const redirect = { prevented: false, preventDefault() { this.prevented = true } }
  handlers.get('will-redirect')(redirect, 'http://127.0.0.1:5173/')
  assert.equal(redirect.prevented, true)
  const webview = { prevented: false, preventDefault() { this.prevented = true } }
  handlers.get('will-attach-webview')(webview)
  assert.equal(webview.prevented, true)
  assert.deepEqual(openHandler({ url: expected }), { action: 'deny' })
})

test('packaged main-page CSP permits only the active API network target', () => {
  const activeApi = 'http://127.0.0.1:43123'
  const csp = buildMainPageCsp({
    apiBaseUrl: activeApi,
    isPackaged: true,
    devServerUrl: 'http://127.0.0.1:5173',
  })
  assert.match(csp, /default-src 'self'/)
  assert.match(csp, /script-src 'self'/)
  assert.match(csp, /style-src 'self'/)
  assert.match(csp, /img-src 'self' data:/)
  assert.match(csp, /connect-src http:\/\/127\.0\.0\.1:43123/)
  assert.match(csp, /font-src 'self'/)
  for (const directive of ['object-src', 'frame-src', 'base-uri', 'form-action']) {
    assert.match(csp, new RegExp(`${directive} 'none'`))
  }
  assert.equal(csp.includes('*'), false)
  assert.equal(csp.includes("'unsafe-eval'"), false)
  assert.equal(csp.includes("'unsafe-inline'"), false)
  assert.equal(csp.includes('5173'), false)

  let listener
  installMainPageCsp(
    { webRequest: { onHeadersReceived: (_filter, callback) => { listener = callback } } },
    { csp, isMainPageUrl: url => url === 'file:///application/dist/index.html' },
  )
  let applied
  listener(
    { url: 'file:///application/dist/index.html', resourceType: 'mainFrame', responseHeaders: {} },
    result => { applied = result.responseHeaders['Content-Security-Policy'][0] },
  )
  assert.equal(applied, csp)
})

test('packaged token ignores an inherited environment token', () => {
  const generated = createApiAuthToken({
    isPackaged: true,
    environmentToken: 'known-development-placeholder',
    randomBytesImpl: size => {
      assert.equal(size, 32)
      return Buffer.alloc(size, 7)
    },
  })
  assert.notEqual(generated, 'known-development-placeholder')
  assert.equal(createApiAuthToken({
    isPackaged: false,
    environmentToken: 'known-development-placeholder',
  }), 'known-development-placeholder')
})

test('failed startup and consecutive retries leave no owned sidecars', async () => {
  const stopped = []
  const supervisor = new BackendSupervisor({
    stopProcess: async child => {
      stopped.push(child.id)
      return true
    },
  })

  for (const id of ['attempt-one', 'attempt-two']) {
    await assert.rejects(runBackendStartupAttempt({
      supervisor,
      launch: async () => ({ baseUrl: 'http://127.0.0.1:40000', process: { id }, owned: true }),
      verify: async () => { throw new BackendStartupError('backend_auth_failed', 'verification failed') },
    }), BackendStartupError)
    assert.equal(supervisor.process, null)
    assert.equal(supervisor.owned, false)
    assert.equal(supervisor.runtime, null)
    assert.equal(supervisor.baseUrl, '')
  }
  assert.deepEqual(stopped, ['attempt-one', 'attempt-two'])
})

test('failed sidecar cleanup retains ownership and blocks replacement startup', async () => {
  let launches = 0
  const supervisor = new BackendSupervisor({ stopProcess: async () => false })
  supervisor.track({
    baseUrl: 'http://127.0.0.1:40001',
    process: { id: 'still-running' },
    owned: true,
  })
  await assert.rejects(
    runBackendStartupAttempt({
      supervisor,
      launch: async () => {
        launches += 1
        return { baseUrl: 'http://127.0.0.1:40002', process: { id: 'replacement' }, owned: true }
      },
      verify: async () => ({}),
    }),
    error => error instanceof BackendStartupError && error.code === 'backend_cleanup_failed',
  )
  assert.equal(launches, 0)
  assert.equal(supervisor.process.id, 'still-running')
  assert.equal(supervisor.owned, true)
  assert.equal(supervisor.runtime, null)
})

test('a stale sidecar exit cannot clear a replacement attempt', () => {
  const supervisor = new BackendSupervisor()
  const oldChild = { id: 'old' }
  const replacement = { id: 'replacement' }
  supervisor.track({ baseUrl: 'http://127.0.0.1:40003', process: replacement, owned: true })
  supervisor.runtime = { service: 'local-ai-agent' }
  assert.equal(supervisor.markExited(oldChild), false)
  assert.equal(supervisor.process, replacement)
  assert.equal(supervisor.runtime.service, 'local-ai-agent')
})

test('process-tree cleanup targets only the supplied owned child pid', async () => {
  const calls = []
  const execFileImpl = (file, args, options, callback) => {
    calls.push({ file, args, options })
    callback(null)
  }
  assert.equal(await stopOwnedProcessTree(null, 'win32', execFileImpl), false)
  assert.equal(await stopOwnedProcessTree({ pid: 4321, killed: false }, 'win32', execFileImpl), true)
  assert.deepEqual(calls, [{
    file: 'taskkill.exe',
    args: ['/pid', '4321', '/T', '/F'],
    options: { windowsHide: true },
  }])
})

test('log sanitizer removes bearer tokens and API key patterns', () => {
  const sanitized = sanitizeLogLine('Authorization: Bearer abc.def api_key=sk-secretvalue')
  assert.equal(sanitized.includes('abc.def'), false)
  assert.equal(sanitized.includes('sk-secretvalue'), false)
})

test('Windows release config separates unsigned development and fail-closed signed release', () => {
  const configPath = path.resolve('electron-builder.p2b.cjs')
  const unsigned = spawnSync(
    process.execPath,
    ['-e', `const c=require(${JSON.stringify(configPath)}); console.log(JSON.stringify({force:c.forceCodeSigning,sign:c.win.signExecutable}))`],
    {
      cwd: path.dirname(configPath),
      encoding: 'utf8',
      env: {
        ...process.env,
        WINDOWS_RELEASE_MODE: 'unsigned-development',
        WINDOWS_SIGN_CERTIFICATE_PATH: '',
        WINDOWS_SIGN_CERTIFICATE_PASSWORD: '',
        WINDOWS_SIGN_TIMESTAMP_URL: '',
        WINDOWS_SIGN_EXPECTED_PUBLISHER: '',
      },
    },
  )
  assert.equal(unsigned.status, 0)
  assert.deepEqual(JSON.parse(unsigned.stdout.trim()), { force: false, sign: false })

  const signedWithoutCertificate = spawnSync(
    process.execPath,
    ['-e', `require(${JSON.stringify(configPath)})`],
    {
      cwd: path.dirname(configPath),
      encoding: 'utf8',
      env: {
        ...process.env,
        WINDOWS_RELEASE_MODE: 'signed-release',
        WINDOWS_SIGN_CERTIFICATE_PATH: '',
        WINDOWS_SIGN_CERTIFICATE_PASSWORD: '',
        WINDOWS_SIGN_TIMESTAMP_URL: '',
        WINDOWS_SIGN_EXPECTED_PUBLISHER: '',
      },
    },
  )
  assert.notEqual(signedWithoutCertificate.status, 0)
  assert.match(signedWithoutCertificate.stderr, /Signed release requires WINDOWS_SIGN_CERTIFICATE_PATH/)
  assert.equal(signedWithoutCertificate.stderr.includes('known-smoke-token'), false)

  const signedConfig = spawnSync(
    process.execPath,
    ['-e', `const c=require(${JSON.stringify(configPath)}); console.log(JSON.stringify(c.win.signtoolOptions))`],
    {
      cwd: path.dirname(configPath),
      encoding: 'utf8',
      env: {
        ...process.env,
        WINDOWS_RELEASE_MODE: 'signed-release',
        WINDOWS_SIGN_CERTIFICATE_PATH: 'C:\\secure\\publisher.pfx',
        WINDOWS_SIGN_CERTIFICATE_PASSWORD: 'never-serialize-this-password',
        WINDOWS_SIGN_TIMESTAMP_URL: 'https://timestamp.invalid',
        WINDOWS_SIGN_EXPECTED_PUBLISHER: 'Expected Publisher',
      },
    },
  )
  assert.equal(signedConfig.status, 0)
  assert.equal(signedConfig.stdout.includes('never-serialize-this-password'), false)
  assert.equal(signedConfig.stdout.includes('publisher.pfx'), false)
})
