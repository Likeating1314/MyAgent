import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'

test('release smoke helpers pass in Windows PowerShell 5.1 and PowerShell 7', () => {
  const script = path.resolve('scripts/test_windows_smoke_helpers.ps1')
  for (const executable of ['powershell.exe', 'pwsh.exe']) {
    const result = spawnSync(executable, [
      '-NoProfile',
      '-ExecutionPolicy', 'Bypass',
      '-File', script,
    ], { encoding: 'utf8', windowsHide: true })
    assert.equal(
      result.status,
      0,
      `${executable} helper contract failed: ${result.stderr || result.stdout}`,
    )
    assert.match(result.stdout, /windows smoke helpers OK/)
  }
})

test('desktop build invalidates prior smoke evidence before build actions', () => {
  const script = readFileSync(path.resolve('scripts/build_desktop.ps1'), 'utf8')
  const invalidation = script.indexOf('Invalidate-WindowsSmokeEvidence')
  const iconBuild = script.indexOf('& npm run icons:build')
  const sidecarBuild = script.indexOf('& npm run sidecar:build')
  assert.ok(invalidation >= 0)
  assert.ok(invalidation < iconBuild)
  assert.ok(invalidation < sidecarBuild)
})
