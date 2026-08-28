import { app, BrowserWindow, ipcMain, safeStorage, session } from 'electron'
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

import {
  appendRotatingLog,
  BackendStartupError,
  BackendSupervisor,
  buildSidecarArgs,
  findAvailablePort,
  runBackendStartupAttempt,
  startupDiagnostic,
  waitForBackend,
} from './backend-runtime.mjs'
import { CredentialStore, registerCredentialIpc } from './credential-store.mjs'
import { AuthService, registerAuthIpc } from './auth-service.mjs'
import {
  buildMainPageCsp,
  createApiAuthToken,
  createMainPageUrlMatcher,
  createRendererAuthorizer,
  installMainPageCsp,
  lockWindowNavigation,
  registerTrustedIpcHandler,
} from './renderer-security.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
if (process.env.LOCAL_AGENT_SMOKE_USER_DATA) {
  app.setPath('userData', path.resolve(process.env.LOCAL_AGENT_SMOKE_USER_DATA))
} else if (app.isPackaged) {
  // Preserve the existing data directory while the public product name changes to MyAgent.
  app.setPath('userData', path.join(app.getPath('appData'), '本地智能体'))
}
const devServerUrl = process.env.VITE_DEV_SERVER_URL || 'http://127.0.0.1:5173'
const apiAuthToken = createApiAuthToken({
  isPackaged: app.isPackaged,
  environmentToken: process.env.API_AUTH_TOKEN,
})
const businessApiBaseUrl = (process.env.BUSINESS_API_BASE_URL || 'http://127.0.0.1:8081').replace(/\/$/, '')
let authService = null
const backendSupervisor = new BackendSupervisor()
const packagedMainPageUrl = pathToFileURL(path.join(app.getAppPath(), 'dist', 'index.html')).href
const isAllowedMainUrl = createMainPageUrlMatcher({
  isPackaged: app.isPackaged,
  packagedPageUrl: packagedMainPageUrl,
  devServerUrl,
})
let mainWindow = null
let diagnosticWindow = null
let diagnosticPageUrl = ''
let quitting = false
let cleanupStarted = false
let startupPromise = null
let logPath = ''

function writeMainLog(message) {
  if (logPath) appendRotatingLog(logPath, message)
}

function backendPaths() {
  const userData = app.getPath('userData')
  const logs = path.join(userData, 'logs')
  mkdirSync(logs, { recursive: true })
  logPath = path.join(logs, 'sidecar.log')
  return {
    workspace: path.join(userData, 'workspace'),
    database: path.join(userData, 'data', 'agent.sqlite3'),
    backendLog: path.join(logs, 'backend.log'),
    credentialFile: path.join(userData, 'credentials', 'api-key.bin'),
    refreshTokenFile: path.join(userData, 'auth', 'refresh-token.bin'),
    authDeviceIdFile: path.join(userData, 'auth', 'device-id'),
  }
}

function pipeSidecarLogs(child) {
  for (const [label, stream] of [['stdout', child.stdout], ['stderr', child.stderr]]) {
    stream?.setEncoding('utf8')
    stream?.on('data', chunk => writeMainLog(`backend ${label}: ${chunk}`))
  }
}

function spawnBackend(port, paths) {
  const explicitUrl = !app.isPackaged ? process.env.VITE_API_BASE_URL : ''
  if (explicitUrl) {
    return { baseUrl: explicitUrl.replace(/\/$/, ''), process: null, owned: false }
  }

  const baseUrl = `http://127.0.0.1:${port}`
  let executable
  let args
  let cwd
  if (app.isPackaged) {
    executable = path.join(process.resourcesPath, 'backend', 'local-agent-backend.exe')
    args = buildSidecarArgs(port, process.pid)
    cwd = path.dirname(executable)
  } else {
    const backendDir = path.resolve(app.getAppPath(), '..', 'backend')
    const candidates = [
      process.env.BACKEND_PYTHON,
      path.join(backendDir, '.venv', 'Scripts', 'python.exe'),
    ].filter(Boolean)
    executable = candidates.find(candidate => existsSync(candidate))
    args = ['-m', 'app.sidecar', ...buildSidecarArgs(port, process.pid)]
    cwd = backendDir
  }
  if (!executable || !existsSync(executable)) {
    throw new BackendStartupError(
      'backend_executable_missing',
      app.isPackaged ? '安装包缺少后端 sidecar。请重新安装应用。' : '未找到开发环境 Python。',
    )
  }

  const runtimeEnv = {
    ...process.env,
    API_AUTH_TOKEN: apiAuthToken,
    OPENAI_API_KEY: '',
  }
  if (app.isPackaged) {
    Object.assign(runtimeEnv, {
      WORKSPACE_DIR: paths.workspace,
      SQLITE_PATH: paths.database,
      AGENT_LOG_PATH: paths.backendLog,
      API_CORS_ORIGINS: 'null',
      ALLOW_NON_LOOPBACK_TOKEN_BOOTSTRAP: 'false',
    })
  }
  const child = spawn(executable, args, {
    cwd,
    env: runtimeEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })
  pipeSidecarLogs(child)
  child.once('error', error => {
    writeMainLog(`backend spawn error type=${error?.name || 'Error'}`)
  })
  child.once('exit', (code, signalName) => {
    const wasOwned = backendSupervisor.markExited(child)
    writeMainLog(`backend exited code=${code ?? 'null'} signal=${signalName ?? 'none'}`)
    if (!quitting && wasOwned && mainWindow && !mainWindow.isDestroyed()) {
      showDiagnostic(
        new BackendStartupError('backend_exited', '本地后端意外退出，请重试启动。'),
      )
    }
  })
  return { baseUrl, process: child, owned: true }
}

async function startAndVerifyBackend() {
  if (startupPromise) return startupPromise
  startupPromise = (async () => {
    const paths = backendPaths()
    const runtime = await runBackendStartupAttempt({
      supervisor: backendSupervisor,
      launch: async () => {
        const explicitUrl = !app.isPackaged ? process.env.VITE_API_BASE_URL : ''
        const port = explicitUrl ? 0 : await findAvailablePort()
        return spawnBackend(port, paths)
      },
      verify: baseUrl => waitForBackend({ baseUrl, token: apiAuthToken }),
    })
    writeMainLog(`backend verified service=${runtime.service} version=${runtime.version}`)
    return runtime
  })().finally(() => {
    startupPromise = null
  })
  return startupPromise
}

async function runPackagedSmoke(paths) {
  const headers = {
    'X-Local-Agent-Token': apiAuthToken,
    ...(authService?.accessToken ? { Authorization: `Bearer ${authService.accessToken}` } : {}),
    'Content-Type': 'application/json',
  }
  const sessionId = `packaged-smoke-${randomUUID()}`
  let inheritedTokenRejected = null
  if (process.env.API_AUTH_TOKEN) {
    const inheritedTokenResponse = await fetch(`${backendSupervisor.baseUrl}/api/runtime`, {
      headers: { 'X-Local-Agent-Token': process.env.API_AUTH_TOKEN },
    })
    inheritedTokenRejected = inheritedTokenResponse.status === 401
    if (!inheritedTokenRejected) {
      throw new BackendStartupError(
        'smoke_inherited_token_accepted',
        '冒烟测试检测到生产后端接受了继承的启动令牌。',
      )
    }
  }
  const runtimeResponse = await fetch(`${backendSupervisor.baseUrl}/api/runtime`, {
    headers: { 'X-Local-Agent-Token': apiAuthToken },
  })
  if (!runtimeResponse.ok) throw new BackendStartupError('smoke_runtime_failed', '冒烟测试运行时验证失败。')
  if (!authService?.accessToken) {
    writeFileSync(
      path.join(path.dirname(paths.backendLog), 'smoke-result.json'),
      JSON.stringify({
        ok: true,
        service: backendSupervisor.runtime.service,
        version: backendSupervisor.runtime.version,
        workspace: backendSupervisor.runtime.workspace,
        renderer_initialized: true,
        user_api_skipped: true,
        reason: 'not_authenticated',
        inherited_token_rejected: inheritedTokenRejected,
        electron_pid: process.pid,
        sidecar_pid: backendSupervisor.process?.pid ?? null,
        backend_port: Number(new URL(backendSupervisor.baseUrl).port),
      }),
      'utf8',
    )
    return
  }
  const sessionResponse = await fetch(`${backendSupervisor.baseUrl}/api/sessions`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ session_id: sessionId }),
  })
  if (!sessionResponse.ok) throw new BackendStartupError('smoke_session_failed', '冒烟测试创建会话失败。')
  const chatResponse = await fetch(`${backendSupervisor.baseUrl}/api/chat`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ session_id: sessionId, message: 'packaged smoke test' }),
  })
  if (!chatResponse.ok) throw new BackendStartupError('smoke_chat_failed', '冒烟测试 mock 对话失败。')
  const chat = await chatResponse.json()
  if (chat?.session_id !== sessionId || typeof chat?.answer !== 'string' || !chat.answer.includes('mock Agent')) {
    throw new BackendStartupError('smoke_chat_mismatch', '冒烟测试 mock 对话响应不匹配。')
  }
  writeFileSync(
    path.join(path.dirname(paths.backendLog), 'smoke-result.json'),
    JSON.stringify({
      ok: true,
      service: backendSupervisor.runtime.service,
      version: backendSupervisor.runtime.version,
      workspace: backendSupervisor.runtime.workspace,
      session_id: sessionId,
      mock_chat: true,
      renderer_initialized: true,
      inherited_token_rejected: inheritedTokenRejected,
      electron_pid: process.pid,
      sidecar_pid: backendSupervisor.process?.pid ?? null,
      backend_port: Number(new URL(backendSupervisor.baseUrl).port),
    }),
    'utf8',
  )
}

function runRendererSmoke(paths) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      reject(new BackendStartupError('smoke_renderer_timeout', '冒烟测试前端初始化超时。'))
    }, 15_000)
    createWindow()
    mainWindow.webContents.once('did-finish-load', () => {
      clearTimeout(timeout)
      runPackagedSmoke(paths).then(resolve, reject)
    })
  })
}

function configureBearerInjection() {
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ['http://127.0.0.1/*', 'http://localhost/*'] },
    (details, callback) => {
      const requestHeaders = { ...details.requestHeaders }
      if (backendSupervisor.baseUrl && details.url.startsWith(`${backendSupervisor.baseUrl}/api/`)) {
        requestHeaders['X-Local-Agent-Token'] = apiAuthToken
        if (authService?.accessToken) requestHeaders.Authorization = `Bearer ${authService.accessToken}`
        else delete requestHeaders.Authorization
      }
      callback({ requestHeaders })
    },
  )
}

function windowOptions(preloadName) {
  return {
    width: 1440,
    height: 960,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: '#f5f7fb',
    title: 'MyAgent',
    icon: path.join(__dirname, 'app-icon.png'),
    webPreferences: {
      preload: path.join(__dirname, preloadName),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  }
}

function isAllowedDiagnosticUrl(url) {
  return Boolean(diagnosticPageUrl) && url === diagnosticPageUrl
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus()
    return
  }
  diagnosticWindow?.close()
  mainWindow = new BrowserWindow(windowOptions('preload.cjs'))
  lockWindowNavigation(mainWindow.webContents, isAllowedMainUrl)
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  mainWindow.webContents.on('did-fail-load', (_event, code) => {
    writeMainLog(`renderer load failed code=${code}`)
  })
  mainWindow.webContents.on('preload-error', (_event, _path, error) => {
    writeMainLog(`preload failed type=${error?.name || 'Error'}`)
  })
  if (!app.isPackaged) {
    mainWindow.loadURL(devServerUrl)
    return
  }
  mainWindow.loadURL(packagedMainPageUrl)
}

function showDiagnostic(error) {
  mainWindow?.close()
  const { code, message } = startupDiagnostic(error)
  writeMainLog(`startup diagnostic code=${code} type=${error?.name || 'Error'}`)
  const target = pathToFileURL(path.join(__dirname, 'diagnostic.html'))
  target.searchParams.set('code', code)
  target.searchParams.set('message', message)
  diagnosticPageUrl = target.href
  if (!diagnosticWindow || diagnosticWindow.isDestroyed()) {
    diagnosticWindow = new BrowserWindow({
      ...windowOptions('diagnostic-preload.cjs'),
      width: 720,
      height: 520,
    })
    lockWindowNavigation(diagnosticWindow.webContents, isAllowedDiagnosticUrl)
    diagnosticWindow.on('closed', () => {
      diagnosticWindow = null
      diagnosticPageUrl = ''
    })
  }
  diagnosticWindow.loadURL(diagnosticPageUrl)
}

const authorizeRenderer = createRendererAuthorizer({
  isPackaged: app.isPackaged,
  devServerUrl,
  getMainWindow: () => mainWindow,
  getDiagnosticWindow: () => diagnosticWindow,
  getMainPageUrl: () => packagedMainPageUrl,
  getDiagnosticPageUrl: () => diagnosticPageUrl,
})

app.whenReady().then(async () => {
  const paths = backendPaths()
  const credentialStore = new CredentialStore({ safeStorage, filePath: paths.credentialFile })
  authService = new AuthService({
    safeStorage,
    filePath: paths.refreshTokenFile,
    deviceIdFile: paths.authDeviceIdFile,
    businessBaseUrl: businessApiBaseUrl,
  })
  registerCredentialIpc(ipcMain, credentialStore, authorizeRenderer)
  registerAuthIpc(ipcMain, authService, authorizeRenderer)
  registerTrustedIpcHandler(ipcMain, 'runtime:get', authorizeRenderer, (_event, ...args) => {
    if (args.length) throw new TypeError('runtime:get 不接受参数')
    if (!backendSupervisor.runtime || !backendSupervisor.baseUrl) throw new Error('后端尚未就绪')
    return { apiBaseUrl: backendSupervisor.baseUrl, runtime: backendSupervisor.runtime }
  })
  registerTrustedIpcHandler(ipcMain, 'backend:retry', authorizeRenderer, async (_event, ...args) => {
    if (args.length) throw new TypeError('backend:retry 不接受参数')
    try {
      await startAndVerifyBackend()
      createWindow()
      return { ok: true }
    } catch (error) {
      showDiagnostic(error)
      return { ok: false, code: error?.code || 'backend_start_failed', message: error?.message }
    }
  })
  registerTrustedIpcHandler(ipcMain, 'app:quit', authorizeRenderer, (_event, ...args) => {
    if (args.length) throw new TypeError('app:quit 不接受参数')
    app.quit()
  })
  configureBearerInjection()
  installMainPageCsp(session.defaultSession, {
    csp: () => buildMainPageCsp({
      apiBaseUrl: backendSupervisor.baseUrl,
      isPackaged: app.isPackaged,
      devServerUrl,
    }),
    isMainPageUrl: isAllowedMainUrl,
  })
  try {
    await startAndVerifyBackend()
    if (app.isPackaged && process.env.LOCAL_AGENT_SMOKE_TEST === '1') {
      await runRendererSmoke(paths)
      if (process.env.LOCAL_AGENT_SMOKE_HOLD !== '1') app.quit()
    } else {
      createWindow()
    }
  } catch (error) {
    if (app.isPackaged && process.env.LOCAL_AGENT_SMOKE_TEST === '1') {
      writeMainLog(`packaged smoke failed code=${error?.code || 'smoke_failed'}`)
      app.quit()
    } else {
      showDiagnostic(error)
    }
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (backendSupervisor.runtime) createWindow()
      else showDiagnostic(new BackendStartupError('backend_not_ready', '本地后端尚未就绪。'))
    }
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', event => {
  quitting = true
  if (!backendSupervisor.owned || !backendSupervisor.process || cleanupStarted) return
  event.preventDefault()
  cleanupStarted = true
  backendSupervisor.stopCurrent()
    .catch(() => false)
    .finally(() => app.quit())
})
