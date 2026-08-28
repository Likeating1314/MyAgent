export class RequestLifecycle {
  private current: AbortController | null = null

  start(): AbortController {
    const controller = new AbortController()
    this.current = controller
    return controller
  }

  stop(): boolean {
    if (!this.current || this.current.signal.aborted) {
      return false
    }
    this.current.abort()
    return true
  }

  finish(controller: AbortController) {
    if (this.current === controller) {
      this.current = null
    }
  }

  get active(): boolean {
    return this.current !== null && !this.current.signal.aborted
  }
}
