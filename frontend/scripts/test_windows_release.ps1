param(
    [string]$ReleaseDirectory = "$PSScriptRoot\..\..\.tmp\electron-release-p2c",
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$releaseDir = [IO.Path]::GetFullPath($ReleaseDirectory)
$resultPath = Join-Path $releaseDir "windows-install-smoke.json"
. "$PSScriptRoot\windows_smoke_evidence.ps1"

# Invalidate old success before every preflight so failures cannot inherit stale evidence.
Invalidate-WindowsSmokeEvidence -Path $resultPath
if (-not [IO.Directory]::Exists($releaseDir)) { throw "Release directory was not found" }

$package = Read-Utf8Json -Path "$PSScriptRoot\..\package.json"
$productName = [string]$package.build.productName
$manifestPath = Join-Path $releaseDir "release-manifest.json"
$binding = Get-WindowsReleaseBinding -ManifestPath $manifestPath -ReleaseDirectory $releaseDir
if ($binding.Version -ne [string]$package.version) { throw "Release manifest version does not match package version" }

function Wait-Until {
    param([scriptblock]$Condition, [string]$FailureMessage)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw $FailureMessage
}

function Test-ProcessExists([int]$Id) {
    return $null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue)
}

function Test-SidecarProcess([int]$Id, [string]$ExpectedExecutable) {
    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    try {
        return $process.Path -eq $ExpectedExecutable
    } catch {
        return $true
    }
}

function Test-LoopbackPortOpen([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connect.Wait(250)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Read-PackagedSmokeResult([string]$UserData) {
    $path = Join-Path $UserData "logs\smoke-result.json"
    Wait-Until { Test-Path -LiteralPath $path } "Packaged smoke result was not written"
    $result = Read-Utf8Json -Path $path
    if (-not $result.ok -or -not $result.renderer_initialized) {
        throw "Packaged smoke result did not pass runtime checks"
    }
    if (-not $result.mock_chat -and -not $result.user_api_skipped) {
        throw "Packaged smoke neither tested authenticated user APIs nor recorded a pre-login skip"
    }
    if ($result.service -ne "local-ai-agent" -or $result.version -ne $binding.Version) {
        throw "Packaged backend identity did not match the release manifest"
    }
    if ($result.inherited_token_rejected -ne $true) {
        throw "Packaged backend accepted an inherited development token"
    }
    return $result
}

function Start-IsolatedSmoke([string]$Executable, [string]$UserData, [bool]$Hold) {
    New-Item -ItemType Directory -Path $UserData -Force | Out-Null
    $saved = @{}
    foreach ($name in @("LOCAL_AGENT_SMOKE_TEST", "LOCAL_AGENT_SMOKE_HOLD", "LOCAL_AGENT_SMOKE_USER_DATA", "API_AUTH_TOKEN", "BACKEND_PYTHON")) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    try {
        $env:LOCAL_AGENT_SMOKE_TEST = "1"
        $env:LOCAL_AGENT_SMOKE_HOLD = if ($Hold) { "1" } else { "0" }
        $env:LOCAL_AGENT_SMOKE_USER_DATA = $UserData
        $env:API_AUTH_TOKEN = "known-smoke-token-that-packaged-mode-must-ignore"
        $env:BACKEND_PYTHON = "Z:\intentionally-missing-python.exe"
        return Start-Process -FilePath $Executable -PassThru
    } finally {
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
        }
    }
}

if (Test-WindowsProductInstallation `
    -ProductName $productName `
    -Version $binding.Version `
    -AnyInstallDirectory) {
    throw "A registered installation already exists; refusing to overwrite it during isolated smoke"
}

$runId = [Guid]::NewGuid().ToString("N")
$tempRoot = (Resolve-Path "$PSScriptRoot\..\..\.tmp").Path
$installDir = Join-Path $tempRoot "p2c-install-$runId"
$normalUserData = Join-Path $tempRoot "p2c-userdata-normal-$runId"
$forcedUserData = Join-Path $tempRoot "p2c-userdata-forced-$runId"
$application = Join-Path $installDir "$productName.exe"
$uninstaller = Join-Path $installDir "Uninstall $productName.exe"
$packagedSidecar = Join-Path $installDir "resources\backend\local-agent-backend.exe"
$uninstalled = $false

try {
    $install = Start-Process `
        -FilePath $binding.InstallerPath `
        -ArgumentList @("/currentuser", "/S", "/D=$installDir") `
        -Wait `
        -PassThru
    if ($install.ExitCode -ne 0) { throw "NSIS installation failed with exit code $($install.ExitCode)" }
    foreach ($required in @($application, $uninstaller, $packagedSidecar)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Installed file is missing: $required" }
    }

    $installedElectronSha256 = Get-SmokeFileSha256 -Path $application
    $installedSidecarSha256 = Get-SmokeFileSha256 -Path $packagedSidecar
    if ($installedElectronSha256 -ne $binding.ElectronSha256) {
        throw "Installed Electron executable hash does not match the release manifest"
    }
    if ($installedSidecarSha256 -ne $binding.SidecarSha256) {
        throw "Installed sidecar hash does not match the release manifest"
    }

    Wait-Until {
        Test-WindowsProductInstallation `
            -ProductName $productName `
            -Version $binding.Version `
            -InstallDir $installDir
    } "Expected an exact uninstall registry entry for the isolated installation"

    $startMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
    Wait-Until {
        @(Get-ChildItem -LiteralPath $startMenu -Recurse -File -Filter "$productName.lnk" -ErrorAction SilentlyContinue).Count -eq 1
    } "Expected exactly one Start Menu shortcut"
    $shortcut = Get-ChildItem -LiteralPath $startMenu -Recurse -File -Filter "$productName.lnk" -ErrorAction Stop

    $normalProcess = Start-IsolatedSmoke -Executable $application -UserData $normalUserData -Hold $false
    $normal = Read-PackagedSmokeResult $normalUserData
    Wait-Until { -not (Test-ProcessExists ([int]$normal.electron_pid)) } "Electron did not exit after normal smoke"
    Wait-Until { -not (Test-SidecarProcess ([int]$normal.sidecar_pid) $packagedSidecar) } "Sidecar survived normal Electron exit"
    Wait-Until { -not (Test-LoopbackPortOpen ([int]$normal.backend_port)) } "Backend port survived normal Electron exit"
    $normalSidecarRemaining = Test-SidecarProcess ([int]$normal.sidecar_pid) $packagedSidecar
    $normalPortOpen = Test-LoopbackPortOpen ([int]$normal.backend_port)

    $forcedProcess = Start-IsolatedSmoke -Executable $application -UserData $forcedUserData -Hold $true
    $forced = Read-PackagedSmokeResult $forcedUserData
    if ($forcedProcess.Id -ne [int]$forced.electron_pid) {
        throw "Started Electron process identity did not match smoke evidence"
    }
    Stop-Process -Id ([int]$forced.electron_pid) -Force
    Wait-Until { -not (Test-ProcessExists ([int]$forced.electron_pid)) } "Forced Electron process remained alive"
    Wait-Until { -not (Test-SidecarProcess ([int]$forced.sidecar_pid) $packagedSidecar) } "Sidecar survived forced Electron termination"
    Wait-Until { -not (Test-LoopbackPortOpen ([int]$forced.backend_port)) } "Backend port survived forced Electron termination"
    $forcedSidecarRemaining = Test-SidecarProcess ([int]$forced.sidecar_pid) $packagedSidecar
    $forcedPortOpen = Test-LoopbackPortOpen ([int]$forced.backend_port)

    $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @("/currentuser", "/S") -Wait -PassThru
    if ($uninstall.ExitCode -ne 0) { throw "NSIS uninstallation failed with exit code $($uninstall.ExitCode)" }
    Wait-Until { -not (Test-Path -LiteralPath $installDir) } "Installation directory survived uninstall"
    $uninstalled = $true
    Wait-Until {
        -not (Test-WindowsProductInstallation `
            -ProductName $productName `
            -Version $binding.Version `
            -InstallDir $installDir)
    } "Uninstall registry entry survived uninstall"
    Wait-Until { -not (Test-Path -LiteralPath $shortcut.FullName) } "Start Menu shortcut survived uninstall"
    if (-not (Test-Path -LiteralPath "$normalUserData\logs\smoke-result.json") -or
        -not (Test-Path -LiteralPath "$forcedUserData\logs\smoke-result.json")) {
        throw "Uninstall removed isolated userData despite the retention policy"
    }

    $evidence = [ordered]@{
        schema_version = 2
        ok = $true
        generated_at_utc = [DateTime]::UtcNow.ToString("o")
        run_id = $runId
        version = $binding.Version
        release_mode = $binding.ReleaseMode
        installer = [IO.Path]::GetFileName($binding.InstallerPath)
        installer_sha256 = $binding.InstallerSha256
        installed_electron_sha256 = $installedElectronSha256
        installed_sidecar_sha256 = $installedSidecarSha256
        release_manifest_sha256 = $binding.ManifestSha256
        install_directory_removed = $true
        start_menu_shortcut_removed = $true
        uninstall_registry_removed = $true
        user_data_retained = $true
        system_python_required = $false
        normal_exit = [ordered]@{
            electron_pid = [int]$normal.electron_pid
            sidecar_pid = [int]$normal.sidecar_pid
            port = [int]$normal.backend_port
            sidecar_remaining = $normalSidecarRemaining
            port_open = $normalPortOpen
        }
        forced_exit = [ordered]@{
            electron_pid = [int]$forced.electron_pid
            sidecar_pid = [int]$forced.sidecar_pid
            port = [int]$forced.backend_port
            sidecar_remaining = $forcedSidecarRemaining
            port_open = $forcedPortOpen
        }
        remaining_sidecars = [int]$normalSidecarRemaining + [int]$forcedSidecarRemaining
        isolated_user_data = @($normalUserData, $forcedUserData)
    }
    Write-WindowsSmokeEvidenceAtomic -Path $resultPath -Evidence $evidence
    if (-not (Test-WindowsSmokeEvidenceCurrent `
        -EvidencePath $resultPath `
        -ManifestPath $manifestPath `
        -ReleaseDirectory $releaseDir)) {
        Invalidate-WindowsSmokeEvidence -Path $resultPath
        throw "New smoke evidence failed current-release verification"
    }
    Write-Output $resultPath
} finally {
    if (-not $uninstalled -and (Test-Path -LiteralPath $uninstaller)) {
        try {
            $cleanup = Start-Process -FilePath $uninstaller -ArgumentList @("/currentuser", "/S") -Wait -PassThru
            if ($cleanup.ExitCode -ne 0) {
                Write-Warning "Isolated NSIS cleanup returned exit code $($cleanup.ExitCode)"
            }
        } catch {
            Write-Warning "Isolated NSIS cleanup failed; inspect only $installDir"
        }
    }
}
