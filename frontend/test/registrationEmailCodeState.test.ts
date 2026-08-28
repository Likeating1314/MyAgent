import assert from 'node:assert/strict'
import test from 'node:test'
import { createRegistrationEmailCodeController, formatVerificationExpiry } from '../src/stores/registrationEmailCode.ts'

function deferred<T>() {
  let resolve!: (value: T) => void; let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('email change invalidates a pending response and never binds it to the new email', async () => {
  const oldRequest = deferred<{ expiresInSeconds: number; resendAfterSeconds: number }>()
  const controller = createRegistrationEmailCodeController(() => oldRequest.promise)
  const pending = controller.send('old@example.com')
  assert.equal(controller.state.sending, true)
  controller.reset()
  oldRequest.resolve({ expiresInSeconds: 300, resendAfterSeconds: 60 })
  assert.equal(await pending, false)
  assert.equal(controller.state.sentForEmail, '')
  assert.equal(controller.state.cooldown, 0)
  assert.equal(controller.state.message, '')
  assert.equal(controller.state.sending, false)
})

test('mode reset discards a late error without restoring stale UI state', async () => {
  const request = deferred<{ expiresInSeconds: number; resendAfterSeconds: number }>()
  const controller = createRegistrationEmailCodeController(() => request.promise)
  const pending = controller.send('user@example.com')
  controller.reset()
  request.reject(new Error('late SMTP error'))
  assert.equal(await pending, false)
  assert.equal(controller.state.error, '')
  assert.equal(controller.state.sending, false)
})

test('latest email wins when an older response arrives after a replacement request', async () => {
  const oldRequest = deferred<{ expiresInSeconds: number; resendAfterSeconds: number }>()
  const newRequest = deferred<{ expiresInSeconds: number; resendAfterSeconds: number }>()
  const requests = [oldRequest, newRequest]
  const controller = createRegistrationEmailCodeController(() => requests.shift()!.promise)
  const oldPending = controller.send('old@example.com')
  controller.reset()
  const newPending = controller.send('new@example.com')
  newRequest.resolve({ expiresInSeconds: 125, resendAfterSeconds: 2 })
  assert.equal(await newPending, true)
  oldRequest.resolve({ expiresInSeconds: 600, resendAfterSeconds: 60 })
  assert.equal(await oldPending, false)
  assert.equal(controller.state.sentForEmail, 'new@example.com')
  assert.equal(controller.state.cooldown, 2)
  assert.equal(controller.expiryLabel.value, '验证码 2 分 5 秒内有效')
  controller.reset()
})

test('countdown disables resend until it reaches zero and then restores the action', async () => {
  let tick!: () => void; let stopped = 0
  const controller = createRegistrationEmailCodeController(
    async () => ({ expiresInSeconds: 90, resendAfterSeconds: 2 }),
    callback => { tick = callback; return 1 as ReturnType<typeof setInterval> },
    () => { stopped += 1 },
  )
  await controller.send('user@example.com')
  assert.equal(controller.state.cooldown, 2)
  assert.equal(controller.sendLabel.value, '2 秒后重发')
  tick(); assert.equal(controller.sendLabel.value, '1 秒后重发')
  tick(); assert.equal(controller.state.cooldown, 0); assert.equal(controller.sendLabel.value, '发送验证码'); assert.equal(stopped, 1)
})

test('verification expiry text follows arbitrary server seconds', () => {
  assert.equal(formatVerificationExpiry(45), '45 秒')
  assert.equal(formatVerificationExpiry(600), '10 分钟')
  assert.equal(formatVerificationExpiry(601), '10 分 1 秒')
})
