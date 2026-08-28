export function createMainBridge(invoke, platform) {
  return Object.freeze({
    platform,
    getRuntimeConfig: () => invoke('runtime:get'),
    credentials: Object.freeze({
      load: () => invoke('credentials:load'),
      save: apiKey => invoke('credentials:save', apiKey),
      delete: () => invoke('credentials:delete'),
    }),
    auth: Object.freeze({
      state: () => invoke('auth:state'),
      restore: () => invoke('auth:restore'),
      register: payload => invoke('auth:register', payload),
      sendRegistrationEmailCode: payload => invoke('auth:send-registration-code', payload),
      login: payload => invoke('auth:login', payload),
      refresh: () => invoke('auth:refresh'),
      logout: () => invoke('auth:logout'),
      me: () => invoke('auth:me'),
    }),
  })
}

export function createDiagnosticBridge(invoke, platform) {
  return Object.freeze({
    platform,
    diagnostics: Object.freeze({
      retry: () => invoke('backend:retry'),
      quit: () => invoke('app:quit'),
    }),
  })
}
