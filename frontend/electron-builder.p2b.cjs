const path = require('node:path')

const releaseMode = process.env.WINDOWS_RELEASE_MODE || 'unsigned-development'
if (!['unsigned-development', 'signed-release'].includes(releaseMode)) {
  throw new Error('WINDOWS_RELEASE_MODE must be unsigned-development or signed-release')
}
const signedRelease = releaseMode === 'signed-release'
const requiredSigningVariables = [
  'WINDOWS_SIGN_CERTIFICATE_PATH',
  'WINDOWS_SIGN_CERTIFICATE_PASSWORD',
  'WINDOWS_SIGN_TIMESTAMP_URL',
  'WINDOWS_SIGN_EXPECTED_PUBLISHER',
]
if (signedRelease) {
  for (const name of requiredSigningVariables) {
    if (!process.env[name]) throw new Error(`Signed release requires ${name}`)
  }
}

module.exports = {
  appId: 'com.local.agent',
  productName: 'MyAgent',
  copyright: 'Copyright © 2026 MyAgent Project',
  forceCodeSigning: signedRelease,
  files: [
    { from: '../.tmp/desktop-renderer-dist', to: 'dist', filter: ['**/*'] },
    { from: '../.tmp/release-assets/app-icon.png', to: 'electron/app-icon.png' },
    'electron/**/*',
    'package.json',
  ],
  directories: { output: '../.tmp/electron-release-p2c', buildResources: '../.tmp/release-assets' },
  extraResources: [
    {
      from: '../.tmp/sidecar-dist/local-agent-backend',
      to: 'backend',
      filter: ['**/*'],
    },
  ],
  win: {
    target: 'nsis',
    icon: '../.tmp/release-assets/app-icon.ico',
    legalTrademarks: 'MyAgent Project',
    verifyUpdateCodeSignature: true,
    signAndEditExecutable: true,
    signExecutable: signedRelease,
    ...(signedRelease ? {
      signtoolOptions: {
        publisherName: process.env.WINDOWS_SIGN_EXPECTED_PUBLISHER,
        rfc3161TimeStampServer: process.env.WINDOWS_SIGN_TIMESTAMP_URL,
        signingHashAlgorithms: ['sha256'],
        sign: path.resolve(__dirname, 'scripts', 'windows_sign_hook.cjs'),
      },
    } : {}),
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: false,
    createStartMenuShortcut: true,
    deleteAppDataOnUninstall: false,
    artifactName: '${productName} Setup ${version}.${ext}',
  },
}
