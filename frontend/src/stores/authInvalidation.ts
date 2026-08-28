type AuthInvalidationHandler = () => void

const handlers = new Set<AuthInvalidationHandler>()
let epoch = 0

export function authSessionEpoch() {
  return epoch
}

export function registerAuthInvalidationHandler(handler: AuthInvalidationHandler) {
  handlers.add(handler)
  return () => handlers.delete(handler)
}

export function invalidateAuthSession() {
  epoch += 1
  for (const handler of handlers) handler()
}
