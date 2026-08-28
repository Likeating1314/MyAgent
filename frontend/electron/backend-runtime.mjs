import { execFile } from 'node:child_process'
import { appendFileSync, existsSync, renameSync, statSync, unlinkSync } from 'node:fs'
import net from 'node:net'

export const EXPECTED_SERVICE = 'local-ai-agent'
export const EXPECTED_VERSION = '0.1.0'

export class BackendStartupError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'BackendStartupError'
    this.code = code
  }
}

export function buildSidecarArgs(port, parentPid) {
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new TypeError('Sidecar port is invalid')
  }
  if (!Number.isInteger(parentPid) || parentPid < 1) {
    throw new TypeError('Electron parent pid is invalid')
  }
  return ['--port', String(port), '--parent-pid', String(parentPid)]
}

export function startupDiagnostic(error) {
  return {
    code: typeof error?.code === 'string' ? error.code : 'backend_start_failed',
    message: error instanceof Error ? error.message : '本地后端启动失败。',
  }
}

export function findAvailablePort(host = '127.0.0.1') {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, host, () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(error => {
        if (error) reject(error)
        else if (!port) reject(new BackendStartupError('port_unavailable', '无法分配本地后端端口。'))
        else resolve(port)
      })
    })
  })
}

async function fetchJson(fetchImpl, url, init, timeoutMs) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetchImpl(url, { ...init, signal: controller.signal })
    if (!response.ok) {
      throw new BackendStartupError(
        response.status === 401 ? 'backend_auth_failed' : 'backend_http_error',
        `本地后端验证失败（HTTP ${response.status}）。`,
      )
    }
    return await response.json()
  } catch (error) {
    if (error instanceof BackendStartupError) throw error
    if (controller.signal.aborted) {
      throw new BackendStartupError('backend_timeout', '本地后端响应超时。')
    }
    throw new BackendStartupError('backend_unreachable', '无法连接本地后端。')
  } finally {
    clearTimeout(timeout)
  }
}

export async function verifyBackend({
  baseUrl,
  token,
  fetchImpl = fetch,
  expectedService = EXPECTED_SERVICE,
  expectedVersion = EXPECTED_VERSION,
  timeoutMs = 1_500,
}) {
  const health = await fetchJson(fetchImpl, `${baseUrl}/health`, {}, timeoutMs)
  if (health?.status !== 'ok' || health?.service !== expectedService || health?.version !== expectedVersion) {
    throw new BackendStartupError(
      'backend_identity_mismatch',
      '端口上的服务不是当前版本的 MyAgent 后端。',
    )
  }
  const runtime = await fetchJson(
    fetchImpl,
    `${baseUrl}/api/runtime`,
    { headers: { 'X-Local-Agent-Token': token } },
    timeoutMs,
  )
  if (
    runtime?.service !== expectedService ||
    runtime?.version !== expectedVersion ||
    typeof runtime?.workspace !== 'string' ||
    runtime?.database?.status !== 'ready'
  ) {
    throw new BackendStartupError(
      'backend_runtime_mismatch',
      '本地后端运行时身份或状态不匹配。',
    )
  }
  return runtime
}

export async function waitForBackend(options, { attempts = 60, intervalMs = 250 } = {}) {
  let lastError = new BackendStartupError('backend_unreachable', '无法连接本地后端。')
  for (let index = 0; index < attempts; index += 1) {
    try {
      return await verifyBackend(options)
    } catch (error) {
      lastError = error
      if (error?.code === 'backend_identity_mismatch' || error?.code === 'backend_auth_failed') {
        throw error
      }
      await new Promise(resolve => setTimeout(resolve, intervalMs))
    }
  }
  throw lastError
}

export function sanitizeLogLine(value) {
  return String(value)
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]')
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[REDACTED_API_KEY]')
    .replace(/(authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi, '$1=[REDACTED]')
    .replace(/[\r\n]+/g, ' ')
    .slice(0, 4_000)
}

export function appendRotatingLog(logPath, value, { maxBytes = 1_000_000, backups = 2 } = {}) {
  if (existsSync(logPath) && statSync(logPath).size >= maxBytes) {
    for (let index = backups; index >= 1; index -= 1) {
      const source = index === 1 ? logPath : `${logPath}.${index - 1}`
      const target = `${logPath}.${index}`
      if (!existsSync(source)) continue
      if (existsSync(target)) unlinkSync(target)
      renameSync(source, target)
    }
  }
  appendFileSync(logPath, `${new Date().toISOString()} ${sanitizeLogLine(value)}\n`, 'utf8')
}

export function stopOwnedProcessTree(child, platform = process.platform, execFileImpl = execFile) {
  if (!child || !child.pid || child.killed) return Promise.resolve(false)
  if (platform !== 'win32') {
    child.kill('SIGTERM')
    return Promise.resolve(true)
  }
  return new Promise(resolve => {
    execFileImpl(
      'taskkill.exe',
      ['/pid', String(child.pid), '/T', '/F'],
      { windowsHide: true },
      error => resolve(!error),
    )
  })
}

export class BackendSupervisor {
  constructor({ stopProcess = stopOwnedProcessTree } = {}) {
    this.stopProcess = stopProcess
    this.process = null
    this.owned = false
    this.baseUrl = ''
    this.runtime = null
  }

  track(attempt) {
    if (!attempt || typeof attempt.baseUrl !== 'string' || !attempt.baseUrl) {
      throw new TypeError('Backend attempt is invalid')
    }
    if (attempt.owned && !attempt.process) throw new TypeError('Owned backend attempt requires a process')
    this.process = attempt.process ?? null
    this.owned = Boolean(attempt.owned)
    this.baseUrl = attempt.baseUrl
    this.runtime = null
  }

  matches(attempt) {
    return Boolean(
      attempt &&
      this.baseUrl === attempt.baseUrl &&
      this.owned === Boolean(attempt.owned) &&
      this.process === (attempt.process ?? null),
    )
  }

  markExited(child) {
    if (!this.owned || !child || this.process !== child) return false
    this.process = null
    this.owned = false
    this.baseUrl = ''
    this.runtime = null
    return true
  }

  async stopCurrent() {
    const child = this.process
    const shouldStop = this.owned && Boolean(child)
    this.baseUrl = ''
    this.runtime = null
    if (!shouldStop) {
      this.process = null
      this.owned = false
      return false
    }
    if (child.exitCode !== null && child.exitCode !== undefined) {
      this.process = null
      this.owned = false
      return true
    }

    let stopped
    try {
      stopped = await this.stopProcess(child)
    } catch (error) {
      if (this.process !== child) return true
      throw new BackendStartupError(
        'backend_cleanup_failed',
        '旧的本地后端未能停止，已阻止新的启动尝试。',
      )
    }
    if (!stopped && this.process === child) {
      throw new BackendStartupError(
        'backend_cleanup_failed',
        '旧的本地后端未能停止，已阻止新的启动尝试。',
      )
    }
    if (this.process === child) {
      this.process = null
      this.owned = false
    }
    return true
  }

  async stopAttempt(attempt) {
    if (!this.matches(attempt)) return false
    return this.stopCurrent()
  }
}

export async function runBackendStartupAttempt({ supervisor, launch, verify }) {
  await supervisor.stopCurrent()
  let attempt = null
  try {
    attempt = await launch()
    supervisor.track(attempt)
    const runtime = await verify(attempt.baseUrl)
    if (!supervisor.matches(attempt)) {
      throw new BackendStartupError('backend_replaced', '本地后端启动状态已失效，请重试。')
    }
    supervisor.runtime = runtime
    return runtime
  } catch (error) {
    if (attempt) await supervisor.stopAttempt(attempt)
    throw error
  }
}
