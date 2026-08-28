param(
    [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe",
    [switch]$SkipInstall,
    [switch]$ValidateReleaseConfiguration
)

$ErrorActionPreference = "Stop"
$backendDir = (Resolve-Path "$PSScriptRoot\..").Path
$repoDir = (Resolve-Path "$backendDir\..").Path
$pythonPath = (Resolve-Path $PythonExe).Path
$distPath = "$repoDir\.tmp\sidecar-dist"
$workPath = "$repoDir\.tmp\sidecar-build"
. "$repoDir\scripts\windows_release.ps1"
$signing = Get-WindowsSigningConfiguration

if ($ValidateReleaseConfiguration) {
    Write-Output $signing.Mode
    exit 0
}

if (-not $SkipInstall) {
    & $pythonPath -m pip install --disable-pip-version-check -r "$backendDir\requirements-sidecar.lock"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install sidecar build dependencies" }
}

Push-Location $backendDir
try {
    & $pythonPath -m PyInstaller --noconfirm --clean --distpath $distPath --workpath $workPath sidecar.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller sidecar build failed" }
} finally {
    Pop-Location
}

$executable = "$distPath\local-agent-backend\local-agent-backend.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Sidecar executable was not generated: $executable"
}
Invoke-WindowsAuthenticodeSign -Path $executable -Configuration $signing
$sidecarEvidence = Get-WindowsArtifactEvidence -Path $executable -Role "sidecar" -BasePath $repoDir
Assert-WindowsArtifactSignature -Evidence $sidecarEvidence -Configuration $signing
Write-Output $executable
