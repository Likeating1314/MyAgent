const parameters = new URLSearchParams(window.location.search)
const message = parameters.get('message')
const code = parameters.get('code')
if (message) document.querySelector('#message').textContent = message
if (code) document.querySelector('#code').textContent = code

const retry = document.querySelector('#retry')
const status = document.querySelector('#status')
retry.addEventListener('click', async () => {
  retry.disabled = true
  status.textContent = '正在重新启动本地后端…'
  const result = await window.desktopApp.diagnostics.retry()
  if (!result.ok) {
    status.textContent = result.message || '重试失败，请检查诊断代码。'
    retry.disabled = false
  }
})
document.querySelector('#quit').addEventListener('click', () => {
  window.desktopApp.diagnostics.quit()
})
