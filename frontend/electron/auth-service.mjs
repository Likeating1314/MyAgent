import { randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const MAX_FIELD = 320

function validateCredentials(payload, register = false) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new TypeError('认证参数无效')
  const allowed = new Set(register ? ['email', 'verificationCode', 'password', 'displayName'] : ['email', 'password'])
  if (Object.keys(payload).some(key => !allowed.has(key))) throw new TypeError('认证参数包含未授权字段')
  if (typeof payload.email !== 'string' || payload.email.length > MAX_FIELD || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.email)) throw new TypeError('邮箱参数无效')
  if (typeof payload.password !== 'string' || payload.password.length < 10 || payload.password.length > 128) throw new TypeError('密码参数无效')
  if (register && (typeof payload.displayName !== 'string' || !payload.displayName.trim() || payload.displayName.length > 80)) throw new TypeError('显示名称无效')
  if (register && (typeof payload.verificationCode !== 'string' || !/^\d{6}$/.test(payload.verificationCode))) throw new TypeError('邮箱验证码无效')
}

function validateEmailCodeRequest(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) || Object.keys(payload).length !== 1 || !Object.hasOwn(payload, 'email')) throw new TypeError('验证码参数无效')
  if (typeof payload.email !== 'string' || payload.email.length > MAX_FIELD || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.email)) throw new TypeError('邮箱参数无效')
}

export class AuthService {
  constructor({ safeStorage, filePath, deviceIdFile, businessBaseUrl, fetchImpl = fetch }) {
    this.safeStorage = safeStorage; this.filePath = filePath; this.businessBaseUrl = businessBaseUrl.replace(/\/$/, ''); this.fetchImpl = fetchImpl
    this.deviceIdFile = deviceIdFile || path.join(path.dirname(filePath), 'device-id')
    this.accessToken = ''; this.expiresAt = ''; this.user = null; this.deviceId = this.loadOrCreateDeviceId(); this.memoryRefreshToken = ''; this.authOperation = 0
  }
  loadOrCreateDeviceId() {
    try {
      const stored = readFileSync(this.deviceIdFile, 'utf8').trim()
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(stored)) return stored
    } catch {}
    const deviceId = randomUUID()
    mkdirSync(path.dirname(this.deviceIdFile), { recursive: true })
    writeFileSync(this.deviceIdFile, deviceId, { encoding: 'utf8', mode: 0o600 })
    return deviceId
  }
  state() { return { authenticated: Boolean(this.accessToken && this.user), expiresAt: this.expiresAt, user: this.user } }
  async businessFetch(path, init) { try { return await this.fetchImpl(`${this.businessBaseUrl}${path}`, init) } catch { throw new Error('无法连接业务后台，请确认服务已启动后重试。') } }
  beginOperation() { this.authOperation += 1; return this.authOperation }
  isCurrentOperation(operation) { return this.authOperation === operation }
  async register(payload) { validateCredentials(payload, true); return this.exchange('/api/v1/auth/register', { ...payload, deviceId: this.deviceId }) }
  async sendRegistrationEmailCode(payload) { validateEmailCodeRequest(payload); const operation=this.authOperation,state=this.state(); const response=await this.businessFetch('/api/v1/auth/register/email-code',{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Client':'electron'},body:JSON.stringify(payload)}); const result=await response.json().catch(()=>({})); if(!response.ok)throw new Error(typeof result.message==='string'?result.message:'验证码发送失败'); if(operation!==this.authOperation||state.authenticated!==this.state().authenticated||state.user!==this.user)throw new Error('登录状态已变化，请重新操作。'); if(!Number.isFinite(result.expiresInSeconds)||!Number.isFinite(result.resendAfterSeconds))throw new Error('认证服务返回无效结果'); return {expiresInSeconds:result.expiresInSeconds,resendAfterSeconds:result.resendAfterSeconds} }
  async login(payload) { validateCredentials(payload); return this.exchange('/api/v1/auth/login', { ...payload, deviceId: this.deviceId }) }
  async restore() { const refreshToken = this.loadRefresh(); if (!refreshToken) return this.state(); const operation = this.beginOperation(); try { return await this.exchange('/api/v1/auth/refresh', { refreshToken, deviceId: this.deviceId }, operation) } catch { if (this.isCurrentOperation(operation)) this.clearCurrent(); return this.state() } }
  async refresh() { const refreshToken = this.loadRefresh(); if (!refreshToken) throw new Error('没有可用的刷新凭据'); return this.exchange('/api/v1/auth/refresh', { refreshToken, deviceId: this.deviceId }) }
  async me() { if (!this.accessToken) return this.state(); const operation = this.authOperation; const token = this.accessToken; const response = await this.fetchImpl(`${this.businessBaseUrl}/api/v1/users/me`, { headers: { Authorization: `Bearer ${token}` } }); if (!response.ok) throw new Error('用户状态读取失败'); const user = await response.json(); if (!this.isCurrentOperation(operation) || this.accessToken !== token) throw new Error('登录状态已变化，请重新操作。'); this.user = user; return this.state() }
  async logout() { const refreshToken = this.loadRefresh(); this.beginOperation(); this.clearCurrent(); await this.fetchImpl(`${this.businessBaseUrl}/api/v1/auth/logout`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Auth-Client': 'electron' }, body: JSON.stringify({ refreshToken }) }); return this.state() }
  async exchange(route, body, operation = this.beginOperation()) { const response = await this.fetchImpl(`${this.businessBaseUrl}${route}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Auth-Client': 'electron' }, body: JSON.stringify(body) }); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(typeof payload.message === 'string' ? payload.message : '认证请求失败'); if (typeof payload.accessToken !== 'string' || typeof payload.refreshToken !== 'string') throw new Error('认证服务返回无效凭据'); if (!this.isCurrentOperation(operation)) throw new Error('登录状态已变化，请重新操作。'); this.accessToken = payload.accessToken; this.expiresAt = payload.expiresAt; this.user = payload.user; this.saveRefresh(payload.refreshToken); return this.state() }
  saveRefresh(value) { if (this.safeStorage.isEncryptionAvailable()) { mkdirSync(path.dirname(this.filePath), { recursive: true }); writeFileSync(this.filePath, this.safeStorage.encryptString(value)); this.memoryRefreshToken = '' } else { this.memoryRefreshToken = value } }
  loadRefresh() { if (this.memoryRefreshToken) return this.memoryRefreshToken; if (!this.safeStorage.isEncryptionAvailable() || !existsSync(this.filePath)) return ''; try { return this.safeStorage.decryptString(readFileSync(this.filePath)) } catch { return '' } }
  clearCurrent() { this.accessToken = ''; this.expiresAt = ''; this.user = null; this.memoryRefreshToken = ''; if (existsSync(this.filePath)) unlinkSync(this.filePath) }
  async clear() { this.beginOperation(); this.clearCurrent() }
}

export function registerAuthIpc(ipcMain, auth, authorize) {
  const handlers = {
    'auth:state': (event, ...args) => { authorize(event, 'auth:state'); if (args.length) throw new TypeError('auth:state 不接受参数'); return auth.state() },
    'auth:restore': (event, ...args) => { authorize(event, 'auth:restore'); if (args.length) throw new TypeError('auth:restore 不接受参数'); return auth.restore() },
    'auth:register': (event, ...args) => { authorize(event, 'auth:register'); if (args.length !== 1) throw new TypeError('auth:register 参数无效'); return auth.register(args[0]) },
    'auth:send-registration-code': (event, ...args) => { authorize(event, 'auth:send-registration-code'); if (args.length !== 1) throw new TypeError('auth:send-registration-code 参数无效'); return auth.sendRegistrationEmailCode(args[0]) },
    'auth:login': (event, ...args) => { authorize(event, 'auth:login'); if (args.length !== 1) throw new TypeError('auth:login 参数无效'); return auth.login(args[0]) },
    'auth:refresh': (event, ...args) => { authorize(event, 'auth:refresh'); if (args.length) throw new TypeError('auth:refresh 不接受参数'); return auth.refresh() },
    'auth:logout': (event, ...args) => { authorize(event, 'auth:logout'); if (args.length) throw new TypeError('auth:logout 不接受参数'); return auth.logout() },
    'auth:me': (event, ...args) => { authorize(event, 'auth:me'); if (args.length) throw new TypeError('auth:me 不接受参数'); return auth.me() },
  }
  for (const [name, handler] of Object.entries(handlers)) ipcMain.handle(name, handler)
}
