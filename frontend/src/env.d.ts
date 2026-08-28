/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_API_AUTH_TOKEN?: string
  readonly VITE_BUSINESS_API_BASE_URL?: string
}

interface Window {
  desktopApp?: {
    platform: string
    getRuntimeConfig(): Promise<{
      apiBaseUrl: string
      runtime: import('./api/client').RuntimeInfo
    }>
    credentials: {
      load(): Promise<CredentialResult>
      save(apiKey: string): Promise<CredentialResult>
      delete(): Promise<CredentialResult>
    }
    auth: {
      state(): Promise<AuthState>
      restore(): Promise<AuthState>
      register(payload: { email: string; verificationCode: string; password: string; displayName: string }): Promise<AuthState>
      sendRegistrationEmailCode(payload: { email: string }): Promise<{ expiresInSeconds: number; resendAfterSeconds: number }>
      login(payload: { email: string; password: string }): Promise<AuthState>
      refresh(): Promise<AuthState>
      logout(): Promise<AuthState>
      me(): Promise<AuthState>
    }
    diagnostics: {
      retry(): Promise<{ ok: boolean; code?: string; message?: string }>
      quit(): Promise<void>
    }
  }
}

interface CredentialResult {
  apiKey: string
  storage: 'encrypted' | 'memory'
  warning: string
}

interface AuthUser { id: string; email: string; displayName: string; status: string; emailVerified: boolean; roles: string[] }
interface AuthState { authenticated: boolean; expiresAt: string; user: AuthUser | null }
