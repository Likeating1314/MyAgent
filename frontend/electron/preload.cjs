const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld(
  'desktopApp',
  Object.freeze({
    platform: process.platform,
    getRuntimeConfig: () => ipcRenderer.invoke('runtime:get'),
    credentials: Object.freeze({
      load: () => ipcRenderer.invoke('credentials:load'),
      save: apiKey => ipcRenderer.invoke('credentials:save', apiKey),
      delete: () => ipcRenderer.invoke('credentials:delete'),
    }),
    auth: Object.freeze({
      state: () => ipcRenderer.invoke('auth:state'),
      restore: () => ipcRenderer.invoke('auth:restore'),
      register: payload => ipcRenderer.invoke('auth:register', payload),
      sendRegistrationEmailCode: payload => ipcRenderer.invoke('auth:send-registration-code', payload),
      login: payload => ipcRenderer.invoke('auth:login', payload),
      refresh: () => ipcRenderer.invoke('auth:refresh'),
      logout: () => ipcRenderer.invoke('auth:logout'),
      me: () => ipcRenderer.invoke('auth:me'),
    }),
  }),
)
