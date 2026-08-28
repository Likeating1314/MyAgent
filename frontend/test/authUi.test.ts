import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('auth form is semantic, accessible and uses 44px interaction targets', () => {
  const vue = readFileSync(new URL('../src/components/AuthGate.vue', import.meta.url), 'utf8')
  const css = readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')
  for (const expected of ['<label', 'autocomplete="email"', 'autocomplete="one-time-code"', 'inputmode="numeric"', 'maxlength="6"', '发送验证码', "'current-password'", "'new-password'", 'role="alert"', 'aria-live="polite"', 'aria-label', 'aria-pressed', ':disabled="authState.submitting"']) assert.ok(vue.includes(expected), expected)
  assert.match(css, /min-height:\s*44px/)
  assert.match(vue, />MyAgent</)
  assert.match(css, /auth-logo-float/)
  assert.match(css, /prefers-reduced-motion:\s*reduce/)
  assert.match(css, /background:\s*var\(--text\)/)
  assert.match(css, /@font-face[\s\S]*Playwrite DE LA Guides[\s\S]*playwrite-de-la-guides\.ttf/)
  assert.match(css, /\.auth-brand h1[\s\S]*font-family:\s*"Playwrite DE LA Guides"/)
  assert.match(css, /\.auth-tabs\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?inset:\s*0;[\s\S]*?height:\s*100%/)
  assert.match(css, /\.auth-tabs\.is-register::before[\s\S]*?translate3d\(calc\(100% \+ 4px\)/)
  assert.match(css, /\.auth-register-slot[\s\S]*?grid-template-rows:\s*0fr[\s\S]*?\.auth-register-slot\.expanded[\s\S]*?grid-template-rows:\s*1fr/)
  assert.match(vue, /:disabled="mode !== 'register'"/)
  assert.match(vue, /sendRegistrationEmailCode/)
  assert.match(vue, /requestedEmail/)
  assert.match(vue, /expiryLabel/)
  assert.doesNotMatch(vue, /验证码 10 分钟内有效/)
  assert.match(css, /\.auth-code-send[\s\S]*min-height:\s*44px|\.auth-tabs button,[\s\S]*\.auth-code-send,[\s\S]*min-height:\s*44px/)
  assert.match(css, /\.auth-card input:focus\s*\{[^}]*border-color:\s*var\(--text\);[^}]*outline:\s*0;[^}]*box-shadow:\s*none;/)
  assert.doesNotMatch(css, /\.auth-card input:focus\s*\{[^}]*background:/)
  assert.doesNotMatch(css, /\.auth-card input:focus-visible\s*,\s*\.auth-card button:focus-visible/)
  assert.equal(/localStorage|sessionStorage/.test(readFileSync(new URL('../src/stores/auth.ts', import.meta.url), 'utf8')), false)
})

test('register fields do not reserve empty verification feedback spacing', () => {
  const vue = readFileSync(new URL('../src/components/AuthGate.vue', import.meta.url), 'utf8')
  assert.match(vue, /v-if="verificationState\.message \|\| verificationState\.cooldown > 0"[^>]*id="verification-feedback"/)
  assert.doesNotMatch(vue, /<p id="verification-feedback"/)
})
