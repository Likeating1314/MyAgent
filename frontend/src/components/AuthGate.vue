<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { authState, login, register, sendRegistrationEmailCode } from '../stores/auth'
import { createRegistrationEmailCodeController } from '../stores/registrationEmailCode'

const mode = ref<'login' | 'register'>('login')
const email = ref(''); const password = ref(''); const displayName = ref(''); const showPassword = ref(false)
const verification = createRegistrationEmailCodeController(sendRegistrationEmailCode)
const { state: verificationState, sendLabel, expiryLabel } = verification
const emit = defineEmits<{ authenticated: [] }>()
const normalizedEmail = computed(() => email.value.trim().toLowerCase())
const validEmail = computed(() => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail.value))
const canSendCode = computed(() => mode.value === 'register' && validEmail.value && !verificationState.sending && verificationState.cooldown === 0)

function switchMode(next: 'login' | 'register') { if (mode.value === next) return; mode.value = next; verification.reset() }
async function sendCode() {
  if (!canSendCode.value) return
  const requestedEmail = normalizedEmail.value
  await verification.send(requestedEmail)
}
async function submit() {
  if (mode.value === 'register') {
    if (!/^\d{6}$/.test(verificationState.verificationCode)) { verificationState.error = '请输入 6 位邮箱验证码。'; return }
    if (verificationState.sentForEmail && verificationState.sentForEmail !== normalizedEmail.value) { verificationState.error = '邮箱已修改，请重新发送验证码。'; return }
    await register(email.value, verificationState.verificationCode, password.value, displayName.value)
  } else await login(email.value, password.value)
  emit('authenticated')
}
watch(normalizedEmail, (next, previous) => { if (previous && next !== previous) verification.reset() })
onBeforeUnmount(verification.reset)
</script>

<template>
  <main class="auth-gate">
    <div class="auth-ambient" aria-hidden="true"></div>
    <section class="auth-shell" aria-labelledby="auth-title">
      <header class="auth-brand">
        <div class="auth-logo" aria-hidden="true"><svg viewBox="0 0 64 64" focusable="false"><path class="auth-logo-frame" d="M13 46V18l19 18 19-18v28" /><path class="auth-logo-core" d="M22 39V28l10 9 10-9v11" /></svg></div>
        <p class="auth-eyebrow">PRIVATE AI WORKSPACE</p><h1 id="auth-title">MyAgent</h1><p class="auth-subtitle">你的本地智能工作台，安全连接每一次对话与协作。</p>
      </header>

      <form class="auth-card" :class="{ 'is-register': mode === 'register' }" novalidate @submit.prevent="submit">
        <div class="auth-tabs" :class="{ 'is-register': mode === 'register' }" role="tablist" aria-label="认证方式">
          <button type="button" role="tab" :aria-selected="mode === 'login'" @click="switchMode('login')">登录</button>
          <button type="button" role="tab" :aria-selected="mode === 'register'" @click="switchMode('register')">注册</button>
        </div>

        <div class="auth-register-slot" :class="{ expanded: mode === 'register' }" :aria-hidden="mode !== 'register'">
          <div class="auth-register-inner">
            <div class="auth-field"><label for="display-name">昵称</label><input id="display-name" v-model="displayName" autocomplete="name" placeholder="输入昵称" :disabled="mode !== 'register'" :required="mode === 'register'" maxlength="80"></div>
          </div>
        </div>

        <div class="auth-field">
          <div class="auth-label-row"><label for="email">邮箱</label><span v-if="mode === 'register'">验证码仅用于本次注册</span></div>
          <div class="auth-email-row" :class="{ 'with-action': mode === 'register' }">
            <input id="email" v-model="email" type="email" autocomplete="email" placeholder="name@example.com" required>
            <button v-if="mode === 'register'" class="auth-code-send" type="button" :disabled="!canSendCode" aria-describedby="verification-feedback" @click="sendCode">{{ sendLabel }}</button>
          </div>
        </div>

        <div class="auth-register-slot auth-code-slot" :class="{ expanded: mode === 'register' }" :aria-hidden="mode !== 'register'">
          <div class="auth-register-inner">
            <div class="auth-field"><label for="verification-code">邮箱验证码</label><input id="verification-code" v-model="verificationState.verificationCode" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" placeholder="6 位数字验证码" :disabled="mode !== 'register'" :required="mode === 'register'" @input="verificationState.verificationCode = verificationState.verificationCode.replace(/\D/g, '').slice(0, 6)"></div>
            <p v-if="verificationState.message || verificationState.cooldown > 0" id="verification-feedback" class="auth-code-feedback" aria-live="polite"><span v-if="verificationState.message">{{ verificationState.message }} {{ expiryLabel }}</span><span v-else>{{ expiryLabel }}</span></p>
            <p v-if="verificationState.error" class="field-error" role="alert">{{ verificationState.error }}</p>
          </div>
        </div>

        <div class="auth-field"><div class="auth-label-row"><label for="password">密码</label><span v-if="mode === 'register'">至少 10 个字符</span></div><div class="password-field"><input id="password" v-model="password" :type="showPassword ? 'text' : 'password'" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" minlength="10" placeholder="输入密码" required><button type="button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" aria-controls="password" @click="showPassword = !showPassword">{{ showPassword ? '隐藏' : '显示' }}</button></div></div>
        <p v-if="authState.error" class="field-error" role="alert">{{ authState.error }}</p>
        <button class="auth-submit" type="submit" :disabled="authState.submitting"><span>{{ authState.submitting ? '请稍候…' : mode === 'login' ? '进入 MyAgent' : '创建账号' }}</span><svg v-if="!authState.submitting" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5" /></svg></button>
      </form>
      <footer class="auth-note"><span aria-hidden="true"></span>数据保留在当前设备，并按登录用户隔离</footer>
    </section>
  </main>
</template>
