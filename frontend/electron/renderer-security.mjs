import { randomBytes } from 'node:crypto'

export const IPC_ROLE = Object.freeze({
  'runtime:get': 'main',
  'credentials:load': 'main',
  'credentials:save': 'main',
  'credentials:delete': 'main',
  'auth:state': 'main',
  'auth:restore': 'main',
  'auth:register': 'main',
  'auth:send-registration-code': 'main',
  'auth:login': 'main',
  'auth:refresh': 'main',
  'auth:logout': 'main',
  'auth:me': 'main',
  'backend:retry': 'diagnostic',
  'app:quit': 'diagnostic',
})

export class TrustedRendererError extends Error {
  constructor() {
    super('IPC sender is not authorized')
    this.name = 'TrustedRendererError'
    this.code = 'ERR_UNTRUSTED_RENDERER'
  }
}

function exactOrigin(value) {
  try {
    const parsed = new URL(value)
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return ''
    return parsed.origin
  } catch {
    return ''
  }
}

function windowIsUsable(window) {
  return Boolean(
    window &&
    typeof window.isDestroyed === 'function' &&
    !window.isDestroyed() &&
    window.webContents &&
    typeof window.webContents.isDestroyed === 'function' &&
    !window.webContents.isDestroyed(),
  )
}

export function createRendererAuthorizer({
  isPackaged,
  devServerUrl,
  getMainWindow,
  getDiagnosticWindow,
  getMainPageUrl,
  getDiagnosticPageUrl,
}) {
  const devOrigin = isPackaged ? '' : exactOrigin(devServerUrl)
  if (!isPackaged && !devOrigin) throw new TypeError('Vite dev server URL must have an exact HTTP(S) origin')

  return function authorizeRenderer(event, capability) {
    const role = IPC_ROLE[capability]
    if (!role) throw new TrustedRendererError()
    const expectedWindow = role === 'main' ? getMainWindow() : getDiagnosticWindow()
    if (!windowIsUsable(expectedWindow)) throw new TrustedRendererError()

    const sender = event?.sender
    const senderFrame = event?.senderFrame
    if (
      !sender ||
      sender !== expectedWindow.webContents ||
      sender.isDestroyed() ||
      !senderFrame ||
      senderFrame !== sender.mainFrame
    ) {
      throw new TrustedRendererError()
    }

    const frameUrl = typeof senderFrame.url === 'string' ? senderFrame.url : ''
    const topUrl = typeof sender.getURL === 'function' ? sender.getURL() : ''
    if (!frameUrl || !topUrl) throw new TrustedRendererError()

    if (role === 'diagnostic') {
      const expected = getDiagnosticPageUrl()
      if (!expected || frameUrl !== expected || topUrl !== expected) throw new TrustedRendererError()
      return
    }

    if (isPackaged) {
      const expected = getMainPageUrl()
      if (!expected || frameUrl !== expected || topUrl !== expected) throw new TrustedRendererError()
      return
    }

    if (exactOrigin(frameUrl) !== devOrigin || exactOrigin(topUrl) !== devOrigin) {
      throw new TrustedRendererError()
    }
  }
}

export function registerTrustedIpcHandler(ipcMain, channel, authorizeRenderer, handler) {
  ipcMain.handle(channel, (event, ...args) => {
    authorizeRenderer(event, channel)
    return handler(event, ...args)
  })
}

export function createMainPageUrlMatcher({ isPackaged, packagedPageUrl, devServerUrl }) {
  if (isPackaged) return url => Boolean(packagedPageUrl) && url === packagedPageUrl
  const devOrigin = exactOrigin(devServerUrl)
  if (!devOrigin) throw new TypeError('Vite dev server URL must have an exact HTTP(S) origin')
  return url => exactOrigin(url) === devOrigin
}

export function lockWindowNavigation(webContents, isAllowedUrl) {
  const blockUnexpectedNavigation = (event, url) => {
    let allowed = false
    try {
      allowed = Boolean(isAllowedUrl(url))
    } catch {
      allowed = false
    }
    if (!allowed) event.preventDefault()
  }
  webContents.on('will-navigate', blockUnexpectedNavigation)
  webContents.on('will-redirect', blockUnexpectedNavigation)
  webContents.on('will-attach-webview', event => event.preventDefault())
  webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
}

function loopbackApiOrigin(apiBaseUrl) {
  try {
    const parsed = new URL(apiBaseUrl)
    if (
      parsed.protocol !== 'http:' ||
      parsed.hostname !== '127.0.0.1' ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== '/' ||
      parsed.search ||
      parsed.hash
    ) {
      return ''
    }
    return parsed.origin
  } catch {
    return ''
  }
}

export function buildMainPageCsp({ apiBaseUrl, isPackaged, devServerUrl }) {
  const apiOrigin = loopbackApiOrigin(`${apiBaseUrl.replace(/\/$/, '')}/`)
  if (!apiOrigin) throw new TypeError('Main page CSP requires an exact loopback API origin')

  const styleSources = ["'self'"]
  const connectSources = [apiOrigin]
  if (!isPackaged) {
    const devOrigin = exactOrigin(devServerUrl)
    if (!devOrigin) throw new TypeError('Main page CSP requires an exact dev server origin')
    const parsed = new URL(devOrigin)
    const websocketOrigin = `${parsed.protocol === 'https:' ? 'wss:' : 'ws:'}//${parsed.host}`
    styleSources.push("'unsafe-inline'")
    connectSources.push(devOrigin, websocketOrigin)
  }

  return [
    "default-src 'self'",
    "script-src 'self'",
    `style-src ${styleSources.join(' ')}`,
    "img-src 'self' data:",
    `connect-src ${[...new Set(connectSources)].join(' ')}`,
    "font-src 'self'",
    "object-src 'none'",
    "frame-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "worker-src 'none'",
  ].join('; ')
}

export function installMainPageCsp(session, { csp, isMainPageUrl }) {
  session.webRequest.onHeadersReceived({ urls: ['<all_urls>'] }, (details, callback) => {
    const responseHeaders = { ...(details.responseHeaders ?? {}) }
    if (details.resourceType === 'mainFrame' && isMainPageUrl(details.url)) {
      const policy = typeof csp === 'function' ? csp() : csp
      if (typeof policy !== 'string' || !policy) throw new TypeError('Main page CSP is unavailable')
      responseHeaders['Content-Security-Policy'] = [policy]
    }
    callback({ responseHeaders })
  })
}

export function createApiAuthToken({ isPackaged, environmentToken, randomBytesImpl = randomBytes }) {
  if (!isPackaged && environmentToken) return environmentToken
  return randomBytesImpl(32).toString('base64url')
}
