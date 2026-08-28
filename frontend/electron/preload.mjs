import { contextBridge, ipcRenderer } from 'electron'

import { createMainBridge } from './preload-bridge.mjs'

contextBridge.exposeInMainWorld(
  'desktopApp',
  createMainBridge((channel, ...args) => ipcRenderer.invoke(channel, ...args), process.platform),
)
