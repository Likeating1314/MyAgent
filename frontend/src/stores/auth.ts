import { reactive } from 'vue'
import { invalidateAuthSession } from './authInvalidation.js'

export interface AuthUser { id: string; email: string; displayName: string; status: string; emailVerified: boolean; roles: string[] }
export interface AuthState { authenticated: boolean; expiresAt: string; user: AuthUser | null }
interface BrowserAuthResult extends AuthState { accessToken: string }
export interface EmailCodeResult { expiresInSeconds: number; resendAfterSeconds: number }

const businessBaseUrl = ((import.meta.env ?? {}).VITE_BUSINESS_API_BASE_URL ?? 'http://127.0.0.1:8081').replace(/\/$/, '')
let webAccessToken = ''
let authOperation = 0
let refreshRequest: { operation: number; promise: Promise<AuthState> } | null = null

export const authState = reactive({ ready: false, authenticated: false, expiresAt: '', user: null as AuthUser | null, submitting: false, error: '' })

function desktopAuth() { return typeof window === 'undefined' ? undefined : window.desktopApp?.auth }
function beginAuthOperation() { authOperation += 1; return authOperation }
function isCurrentOperation(operation: number) { return authOperation === operation }
function staleAuthOperation() { return new Error('登录状态已变化，请重新操作。') }
function resultAccessToken(state: AuthState | BrowserAuthResult) { return 'accessToken' in state && typeof state.accessToken === 'string' ? state.accessToken : undefined }
function apply(state: AuthState) { authState.authenticated = state.authenticated; authState.expiresAt = state.expiresAt || ''; authState.user = state.user }

async function businessFetch(path: string, init: RequestInit) {
  try {
    return await fetch(`${businessBaseUrl}${path}`, init)
  } catch {
    throw new Error('无法连接业务后台，请确认服务已启动后重试。')
  }
}

function commit(operation: number, state: AuthState, browserAccessToken?: string) {
  if (!isCurrentOperation(operation)) throw staleAuthOperation()
  const previousUserId = authState.user?.id ?? null
  const nextUserId = state.user?.id ?? null
  if (previousUserId !== null && previousUserId !== nextUserId) invalidateAuthSession()
  if (browserAccessToken !== undefined) webAccessToken = browserAccessToken
  apply(state)
  return state
}

function clearLocalAuth() {
  webAccessToken = ''
  apply({ authenticated: false, expiresAt: '', user: null })
  invalidateAuthSession()
}

async function browserExchange(path: string, body: object): Promise<BrowserAuthResult> {
  const response = await businessFetch(path, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof payload.message === 'string' ? payload.message : '认证请求失败')
  const accessToken = typeof payload.accessToken === 'string' ? payload.accessToken : ''
  return { accessToken, authenticated: Boolean(accessToken), expiresAt: String(payload.expiresAt ?? ''), user: payload.user ?? null }
}

async function browserJson<T>(path: string, body: object): Promise<T> {
  const response = await businessFetch(path, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof payload.message === 'string' ? payload.message : '请求失败')
  return payload as T
}

export async function initializeAuth() {
  const operation = beginAuthOperation()
  authState.error = ''
  try {
    const desktop = desktopAuth()
    const result = desktop ? await desktop.restore() : await browserExchange('/api/v1/auth/refresh', { deviceId: browserDeviceId() })
    commit(operation, result, resultAccessToken(result))
  } catch {
    if (isCurrentOperation(operation)) { authOperation += 1; clearLocalAuth() }
  } finally { authState.ready = true }
}

export async function login(email: string, password: string) { return submit(async () => { const desktop = desktopAuth(); return desktop ? desktop.login({ email, password }) : browserExchange('/api/v1/auth/login', { email, password, deviceId: browserDeviceId() }) }) }
export async function sendRegistrationEmailCode(email: string): Promise<EmailCodeResult> { const desktop = desktopAuth(); return desktop ? desktop.sendRegistrationEmailCode({ email }) : browserJson('/api/v1/auth/register/email-code', { email }) }
export async function register(email: string, verificationCode: string, password: string, displayName: string) { return submit(async () => { const desktop = desktopAuth(); return desktop ? desktop.register({ email, verificationCode, password, displayName }) : browserExchange('/api/v1/auth/register', { email, verificationCode, password, displayName, deviceId: browserDeviceId() }) }) }
export async function logout() { const desktop = desktopAuth(); clearAuth(); if (desktop) await desktop.logout(); else await fetch(`${businessBaseUrl}/api/v1/auth/logout`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: '{}' }) }

async function submit(action: () => Promise<AuthState | BrowserAuthResult>) {
  const operation = beginAuthOperation()
  authState.submitting = true; authState.error = ''
  try {
    const state = await action()
    return commit(operation, state, resultAccessToken(state))
  } catch (error) {
    if (isCurrentOperation(operation)) authState.error = error instanceof Error ? error.message : '认证失败'
    throw error
  } finally { if (isCurrentOperation(operation)) authState.submitting = false }
}

export async function refreshAuth() {
  if (refreshRequest && isCurrentOperation(refreshRequest.operation)) return refreshRequest.promise
  const operation = beginAuthOperation()
  const desktop = desktopAuth()
  const current = { operation, promise: Promise.resolve({ authenticated: false, expiresAt: '', user: null } as AuthState) }
  current.promise = (desktop ? desktop.refresh() : browserExchange('/api/v1/auth/refresh', { deviceId: browserDeviceId() }))
    .then(state => commit(operation, state, resultAccessToken(state)))
    .catch(async error => {
      if (!isCurrentOperation(operation)) throw error
      authOperation += 1
      clearLocalAuth()
      try {
        if (desktop) await desktop.logout()
        else await fetch(`${businessBaseUrl}/api/v1/auth/logout`, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      } catch { /* already invalidated locally */ }
      throw error
    })
    .finally(() => { if (refreshRequest === current) refreshRequest = null })
  refreshRequest = current
  return current.promise
}

export function accessToken() { return webAccessToken }
export function clearAuth() { beginAuthOperation(); clearLocalAuth() }
function browserDeviceId() { return 'browser-session' }
