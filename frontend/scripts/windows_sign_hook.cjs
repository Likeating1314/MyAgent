const { spawnSync } = require('node:child_process')
const path = require('node:path')

module.exports = async function signWindowsArtifact(configuration) {
  const required = [
    'WINDOWS_SIGNTOOL_PATH',
    'WINDOWS_SIGN_CERTIFICATE_PATH',
    'WINDOWS_SIGN_CERTIFICATE_PASSWORD',
    'WINDOWS_SIGN_TIMESTAMP_URL',
  ]
  for (const name of required) {
    if (!process.env[name]) throw new Error(`Windows signing hook requires ${name}`)
  }
  if (!configuration?.path || path.extname(configuration.path).toLowerCase() !== '.exe') {
    throw new Error('Windows signing hook only accepts an executable artifact')
  }

  const result = spawnSync(process.env.WINDOWS_SIGNTOOL_PATH, [
    'sign',
    '/fd', 'SHA256',
    '/td', 'SHA256',
    '/tr', process.env.WINDOWS_SIGN_TIMESTAMP_URL,
    '/f', process.env.WINDOWS_SIGN_CERTIFICATE_PATH,
    '/p', process.env.WINDOWS_SIGN_CERTIFICATE_PASSWORD,
    configuration.path,
  ], {
    windowsHide: true,
    stdio: 'ignore',
  })
  if (result.status !== 0) {
    throw new Error(`Authenticode signing failed for ${path.basename(configuration.path)}`)
  }
}
