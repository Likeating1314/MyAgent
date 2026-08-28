const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld(
  'desktopApp',
  Object.freeze({
    platform: process.platform,
    diagnostics: Object.freeze({
      retry: () => ipcRenderer.invoke('backend:retry'),
      quit: () => ipcRenderer.invoke('app:quit'),
    }),
  }),
)
