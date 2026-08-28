import { computed, reactive } from 'vue'
import type { EmailCodeResult } from './auth'

type TimerHandle = ReturnType<typeof setInterval>
type StartTimer = (callback: () => void, milliseconds: number) => TimerHandle
type StopTimer = (timer: TimerHandle) => void

export function formatVerificationExpiry(seconds: number) {
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60); const remainder = total % 60
  return remainder === 0 ? `${minutes} 分钟` : `${minutes} 分 ${remainder} 秒`
}

export function createRegistrationEmailCodeController(
  sendRequest: (email: string) => Promise<EmailCodeResult>,
  startTimer: StartTimer = setInterval,
  stopTimer: StopTimer = clearInterval,
) {
  const state = reactive({ verificationCode: '', sending: false, message: '', error: '', cooldown: 0, expiresInSeconds: 0, sentForEmail: '' })
  let requestGeneration = 0
  let countdownTimer: TimerHandle | undefined

  const sendLabel = computed(() => state.sending ? '发送中…' : state.cooldown > 0 ? `${state.cooldown} 秒后重发` : '发送验证码')
  const expiryLabel = computed(() => state.expiresInSeconds > 0 ? `验证码 ${formatVerificationExpiry(state.expiresInSeconds)}内有效` : '')

  function stopCountdown() { if (countdownTimer !== undefined) stopTimer(countdownTimer); countdownTimer = undefined }
  function reset() { requestGeneration += 1; stopCountdown(); state.verificationCode = ''; state.sending = false; state.message = ''; state.error = ''; state.cooldown = 0; state.expiresInSeconds = 0; state.sentForEmail = '' }
  function startCountdown(seconds: number) { stopCountdown(); state.cooldown = Math.max(0, Math.floor(seconds)); if (state.cooldown === 0) return; countdownTimer = startTimer(() => { state.cooldown = Math.max(0, state.cooldown - 1); if (state.cooldown === 0) stopCountdown() }, 1000) }
  async function send(requestedEmail: string) {
    const generation = ++requestGeneration
    state.sending = true; state.error = ''; state.message = ''
    try {
      const result = await sendRequest(requestedEmail)
      if (generation !== requestGeneration) return false
      state.sentForEmail = requestedEmail; state.expiresInSeconds = Math.max(0, Math.floor(result.expiresInSeconds)); state.message = '验证码已发送，请检查邮箱。'; startCountdown(result.resendAfterSeconds)
      return true
    } catch (error) {
      if (generation !== requestGeneration) return false
      state.error = error instanceof Error ? error.message : '验证码发送失败'
      return false
    } finally {
      if (generation === requestGeneration) state.sending = false
    }
  }
  return { state, sendLabel, expiryLabel, send, reset, stopCountdown }
}
