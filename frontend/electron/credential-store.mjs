import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'

const MEMORY_WARNING = '系统安全存储不可用，API Key 仅保留到本次应用退出。'

export class CredentialStore {
  constructor({ safeStorage, filePath }) {
    this.safeStorage = safeStorage
    this.filePath = filePath
    this.memoryValue = ''
  }

  isEncryptionAvailable() {
    return Boolean(this.safeStorage?.isEncryptionAvailable())
  }

  async load() {
    if (this.memoryValue) return this.#result(this.memoryValue)
    if (!this.isEncryptionAvailable()) return this.#result('')
    try {
      const encrypted = await readFile(this.filePath)
      this.memoryValue = this.safeStorage.decryptString(encrypted)
    } catch (error) {
      if (error?.code !== 'ENOENT') await rm(this.filePath, { force: true }).catch(() => undefined)
      this.memoryValue = ''
    }
    return this.#result(this.memoryValue)
  }

  async save(value) {
    if (typeof value !== 'string' || value.length > 16_384 || value.includes('\0')) {
      throw new TypeError('API Key 参数无效')
    }
    if (!value) return this.delete()
    this.memoryValue = value
    if (!this.isEncryptionAvailable()) return this.#result(value)
    const encrypted = this.safeStorage.encryptString(value)
    await mkdir(path.dirname(this.filePath), { recursive: true })
    const temporary = `${this.filePath}.tmp`
    await writeFile(temporary, encrypted, { mode: 0o600 })
    await rm(this.filePath, { force: true })
    await rename(temporary, this.filePath)
    return this.#result(value)
  }

  async delete() {
    this.memoryValue = ''
    await rm(this.filePath, { force: true })
    return this.#result('')
  }

  #result(apiKey) {
    const encrypted = this.isEncryptionAvailable()
    return {
      apiKey,
      storage: encrypted ? 'encrypted' : 'memory',
      warning: encrypted ? '' : MEMORY_WARNING,
    }
  }
}

export function registerCredentialIpc(ipcMain, store, authorizeRenderer) {
  ipcMain.handle('credentials:load', (event, ...args) => {
    authorizeRenderer(event, 'credentials:load')
    if (args.length) throw new TypeError('credentials:load 不接受参数')
    return store.load()
  })
  ipcMain.handle('credentials:save', (event, ...args) => {
    authorizeRenderer(event, 'credentials:save')
    if (args.length !== 1) throw new TypeError('credentials:save 参数数量无效')
    if (typeof args[0] !== 'string' || args[0].length > 16_384 || args[0].includes('\0')) {
      throw new TypeError('credentials:save 参数无效')
    }
    return store.save(args[0])
  })
  ipcMain.handle('credentials:delete', (event, ...args) => {
    authorizeRenderer(event, 'credentials:delete')
    if (args.length) throw new TypeError('credentials:delete 不接受参数')
    return store.delete()
  })
}
