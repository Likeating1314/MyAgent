param([string]$HelperPath = "$PSScriptRoot\windows_smoke_evidence.ps1")

$ErrorActionPreference = "Stop"
. $HelperPath

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$randomProduct = "MyAgent Registry Probe $([Guid]::NewGuid().ToString('N'))"
Assert-True `
    (-not (Test-WindowsProductInstallation -ProductName $randomProduct -Version "0.0.0" -AnyInstallDirectory)) `
    "an absent product must return false without throwing"

$installDir = [IO.Path]::Combine([IO.Path]::GetTempPath(), "agent-registry-exact")
$differentInstallDir = "$($installDir)-other"
$registeredInstallDir = $installDir
$exactReader = {
    @([pscustomobject]@{
        RegistryLocation = "TEST\exact"
        DisplayName = "MyAgent Test 1.2.3"
        InstallLocation = $registeredInstallDir
        UninstallString = "`"$registeredInstallDir\Uninstall MyAgent Test.exe`" /currentuser"
        QuietUninstallString = "`"$registeredInstallDir\Uninstall MyAgent Test.exe`" /currentuser /S"
    })
}.GetNewClosure()
Assert-True `
    (Test-WindowsProductInstallation `
        -ProductName "MyAgent Test" `
        -Version "1.2.3" `
        -InstallDir $installDir `
        -RegistryReader $exactReader) `
    "an exact product and install directory must return true"
Assert-True `
    (-not (Test-WindowsProductInstallation `
        -ProductName "MyAgent Test" `
        -Version "1.2.3" `
        -InstallDir $differentInstallDir `
        -RegistryReader $exactReader)) `
    "a different install directory must not match"

$uninstallFallbackReader = {
    @([pscustomobject]@{
        RegistryLocation = "TEST\fallback"
        DisplayName = "MyAgent Test 1.2.3"
        InstallLocation = $null
        UninstallString = "`"$registeredInstallDir\Uninstall MyAgent Test.exe`" /currentuser"
        QuietUninstallString = $null
    })
}.GetNewClosure()
Assert-True `
    (Test-WindowsProductInstallation `
        -ProductName "MyAgent Test" `
        -Version "1.2.3" `
        -InstallDir $installDir `
        -RegistryReader $uninstallFallbackReader) `
    "the uninstall executable directory may be used when InstallLocation is absent"
Assert-True `
    (-not (Test-WindowsProductInstallation `
        -ProductName "MyAgent Test" `
        -Version "1.2.3" `
        -InstallDir $differentInstallDir `
        -RegistryReader $uninstallFallbackReader)) `
    "the uninstall executable fallback must still require an exact directory"

$accessFailedClosed = $false
try {
    $null = Test-WindowsProductInstallation `
        -ProductName "MyAgent Test" `
        -Version "1.2.3" `
        -AnyInstallDirectory `
        -RegistryReader { throw [UnauthorizedAccessException]::new("registry denied") }
} catch [UnauthorizedAccessException] {
    $accessFailedClosed = $true
}
Assert-True $accessFailedClosed "registry access failure must propagate fail-closed"

$testRoot = [IO.Path]::Combine([IO.Path]::GetTempPath(), "agent-smoke-helper-$([Guid]::NewGuid().ToString('N'))")
[IO.Directory]::CreateDirectory($testRoot) | Out-Null
try {
    $evidencePath = Join-Path $testRoot "windows-install-smoke.json"
    [IO.File]::WriteAllText($evidencePath, '{"ok":true}', [Text.UTF8Encoding]::new($false))
    Invalidate-WindowsSmokeEvidence -Path $evidencePath
    $preflightFailed = $false
    try {
        $null = Test-WindowsProductInstallation `
            -ProductName "MyAgent Test" `
            -Version "1.2.3" `
            -AnyInstallDirectory `
            -RegistryReader { throw [UnauthorizedAccessException]::new("intentional preflight failure") }
    } catch [UnauthorizedAccessException] {
        $preflightFailed = $true
    }
    Assert-True $preflightFailed "intentional preflight must fail"
    Assert-True (-not [IO.File]::Exists($evidencePath)) "old success evidence must remain invalid after preflight failure"

    $releaseDir = Join-Path $testRoot "release"
    $electronDir = Join-Path $releaseDir "win-unpacked"
    $sidecarDir = Join-Path $electronDir "resources\backend"
    [IO.Directory]::CreateDirectory($sidecarDir) | Out-Null
    $installerPath = Join-Path $releaseDir "MyAgent Setup.exe"
    $electronPath = Join-Path $electronDir "MyAgent.exe"
    $sidecarPath = Join-Path $sidecarDir "local-agent-backend.exe"
    [IO.File]::WriteAllText($installerPath, "installer-v1", [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText($electronPath, "electron-v1", [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText($sidecarPath, "sidecar-v1", [Text.Encoding]::ASCII)
    $manifestPath = Join-Path $releaseDir "release-manifest.json"
    $manifest = [ordered]@{
        version = "1.2.3"
        release_mode = "unsigned-development"
        artifacts = @(
            [ordered]@{ role = "nsis_installer"; path = "MyAgent Setup.exe"; sha256 = Get-SmokeFileSha256 $installerPath },
            [ordered]@{ role = "electron_executable"; path = "win-unpacked/MyAgent.exe"; sha256 = Get-SmokeFileSha256 $electronPath },
            [ordered]@{ role = "sidecar"; path = "win-unpacked/resources/backend/local-agent-backend.exe"; sha256 = Get-SmokeFileSha256 $sidecarPath }
        )
    }
    [IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 6),
        [Text.UTF8Encoding]::new($false)
    )
    $binding = Get-WindowsReleaseBinding -ManifestPath $manifestPath -ReleaseDirectory $releaseDir
    $incompleteEvidence = [ordered]@{
        ok = $true
        version = $binding.Version
        release_mode = $binding.ReleaseMode
        release_manifest_sha256 = $binding.ManifestSha256
        installer_sha256 = $binding.InstallerSha256
        installed_electron_sha256 = $binding.ElectronSha256
        installed_sidecar_sha256 = $binding.SidecarSha256
    }
    Write-WindowsSmokeEvidenceAtomic -Path $evidencePath -Evidence $incompleteEvidence
    Assert-True `
        (-not (Test-WindowsSmokeEvidenceCurrent -EvidencePath $evidencePath -ManifestPath $manifestPath -ReleaseDirectory $releaseDir)) `
        "evidence without a schema, run id and UTC generation time must be rejected"
    Invalidate-WindowsSmokeEvidence -Path $evidencePath

    $evidence = [ordered]@{
        schema_version = 2
        ok = $true
        generated_at_utc = [DateTime]::UtcNow.ToString("o")
        run_id = [Guid]::NewGuid().ToString("N")
        version = $binding.Version
        release_mode = $binding.ReleaseMode
        release_manifest_sha256 = $binding.ManifestSha256
        installer_sha256 = $binding.InstallerSha256
        installed_electron_sha256 = $binding.ElectronSha256
        installed_sidecar_sha256 = $binding.SidecarSha256
    }
    Write-WindowsSmokeEvidenceAtomic -Path $evidencePath -Evidence $evidence
    Assert-True `
        (Test-WindowsSmokeEvidenceCurrent -EvidencePath $evidencePath -ManifestPath $manifestPath -ReleaseDirectory $releaseDir) `
        "fresh evidence must match its release"

    [IO.File]::AppendAllText($installerPath, "tampered", [Text.Encoding]::ASCII)
    $tamperRejected = $false
    try {
        $tamperRejected = -not (Test-WindowsSmokeEvidenceCurrent `
            -EvidencePath $evidencePath `
            -ManifestPath $manifestPath `
            -ReleaseDirectory $releaseDir)
    } catch [IO.InvalidDataException] {
        $tamperRejected = $true
    }
    Assert-True $tamperRejected "tampered installer must invalidate old smoke evidence"
} finally {
    $safeTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith($safeTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a smoke helper directory outside the system temp directory"
    }
    if ([IO.Directory]::Exists($resolvedTestRoot)) { [IO.Directory]::Delete($resolvedTestRoot, $true) }
}

Write-Output "windows smoke helpers OK PowerShell=$($PSVersionTable.PSVersion) Edition=$($PSVersionTable.PSEdition)"
