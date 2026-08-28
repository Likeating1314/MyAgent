$ErrorActionPreference = "Stop"

function Get-SmokeFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    $stream = [IO.File]::OpenRead($resolved)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Read-Utf8Json {
    param([Parameter(Mandatory = $true)][string]$Path)
    $json = [IO.File]::ReadAllText([IO.Path]::GetFullPath($Path), [Text.Encoding]::UTF8)
    return $json | ConvertFrom-Json
}

function Get-WindowsUninstallRecords {
    param([scriptblock]$RegistryReader)
    if ($null -ne $RegistryReader) {
        return @(& $RegistryReader)
    }

    $targets = @(
        [pscustomobject]@{ Hive = [Microsoft.Win32.RegistryHive]::CurrentUser; View = [Microsoft.Win32.RegistryView]::Registry32; Label = "HKCU32" },
        [pscustomobject]@{ Hive = [Microsoft.Win32.RegistryHive]::LocalMachine; View = [Microsoft.Win32.RegistryView]::Registry32; Label = "HKLM32" }
    )
    if ([Environment]::Is64BitOperatingSystem) {
        $targets += @(
            [pscustomobject]@{ Hive = [Microsoft.Win32.RegistryHive]::CurrentUser; View = [Microsoft.Win32.RegistryView]::Registry64; Label = "HKCU64" },
            [pscustomobject]@{ Hive = [Microsoft.Win32.RegistryHive]::LocalMachine; View = [Microsoft.Win32.RegistryView]::Registry64; Label = "HKLM64" }
        )
    }

    $records = @()
    $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall"
    foreach ($target in $targets) {
        $baseKey = $null
        $uninstallKey = $null
        try {
            $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey($target.Hive, $target.View)
            $uninstallKey = $baseKey.OpenSubKey($uninstallPath, $false)
            if ($null -eq $uninstallKey) { continue }
            foreach ($subKeyName in $uninstallKey.GetSubKeyNames()) {
                $entry = $null
                try {
                    $entry = $uninstallKey.OpenSubKey($subKeyName, $false)
                    if ($null -eq $entry) {
                        throw [IO.IOException]::new("Uninstall registry entry changed while it was being inspected")
                    }
                    $values = @{}
                    foreach ($field in @("DisplayName", "InstallLocation", "UninstallString", "QuietUninstallString")) {
                        $value = $entry.GetValue(
                            $field,
                            $null,
                            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
                        )
                        if ($null -ne $value -and $value -isnot [string]) {
                            throw [IO.InvalidDataException]::new("Uninstall registry field $field is not a string")
                        }
                        $values[$field] = $value
                    }
                    $records += [pscustomobject]@{
                        RegistryLocation = "$($target.Label)\$subKeyName"
                        DisplayName = $values.DisplayName
                        InstallLocation = $values.InstallLocation
                        UninstallString = $values.UninstallString
                        QuietUninstallString = $values.QuietUninstallString
                    }
                } finally {
                    if ($null -ne $entry) { $entry.Dispose() }
                }
            }
        } finally {
            if ($null -ne $uninstallKey) { $uninstallKey.Dispose() }
            if ($null -ne $baseKey) { $baseKey.Dispose() }
        }
    }
    return @($records)
}

function Get-UninstallExecutablePath {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $null }
    $trimmed = $CommandLine.Trim()
    if ($trimmed.StartsWith('"')) {
        $closingQuote = $trimmed.IndexOf('"', 1)
        if ($closingQuote -lt 2) {
            throw [IO.InvalidDataException]::new("Quoted uninstall command is malformed")
        }
        return $trimmed.Substring(1, $closingQuote - 1)
    }
    $match = [Text.RegularExpressions.Regex]::Match(
        $trimmed,
        "^(.*?\.exe)(?:\s|$)",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if (-not $match.Success) {
        throw [IO.InvalidDataException]::new("Uninstall command does not contain an executable path")
    }
    return $match.Groups[1].Value
}

function Test-WindowsProductInstallation {
    param(
        [Parameter(Mandatory = $true)][string]$ProductName,
        [Parameter(Mandatory = $true)][string]$Version,
        [string]$InstallDir,
        [switch]$AnyInstallDirectory,
        [scriptblock]$RegistryReader
    )
    $expectedDisplayName = "$ProductName $Version"
    $records = Get-WindowsUninstallRecords -RegistryReader $RegistryReader
    $matchingProducts = @($records | Where-Object {
        [string]::Equals($_.DisplayName, $expectedDisplayName, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($AnyInstallDirectory) { return $matchingProducts.Count -gt 0 }
    if ([string]::IsNullOrWhiteSpace($InstallDir)) {
        throw [ArgumentException]::new("InstallDir is required for an exact installation check")
    }

    $expectedDirectory = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    foreach ($record in $matchingProducts) {
        if (-not [string]::IsNullOrWhiteSpace($record.InstallLocation)) {
            $registeredDirectory = [IO.Path]::GetFullPath($record.InstallLocation).TrimEnd('\')
            if ([string]::Equals($registeredDirectory, $expectedDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
            # An explicit InstallLocation is authoritative. Do not let a stale or
            # unrelated uninstall command turn a directory mismatch into a match.
            continue
        }
        foreach ($commandLine in @($record.UninstallString, $record.QuietUninstallString)) {
            $executable = Get-UninstallExecutablePath -CommandLine $commandLine
            if ($null -eq $executable) { continue }
            $registeredDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($executable)).TrimEnd('\')
            if ([string]::Equals($registeredDirectory, $expectedDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Invalidate-WindowsSmokeEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path)
    if ([IO.File]::Exists($resolved)) { [IO.File]::Delete($resolved) }
    if ([IO.File]::Exists($resolved)) {
        throw [IO.IOException]::new("Previous smoke evidence could not be invalidated")
    }
}

function Resolve-ReleaseArtifactPath {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseDirectory,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw [IO.InvalidDataException]::new("Release manifest artifact path must be relative")
    }
    $base = [IO.Path]::GetFullPath($ReleaseDirectory).TrimEnd('\') + '\'
    $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($base, $RelativePath.Replace('/', '\')))
    if (-not $candidate.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
        throw [IO.InvalidDataException]::new("Release manifest artifact escapes the release directory")
    }
    return $candidate
}

function Get-WindowsReleaseBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ReleaseDirectory
    )
    $manifest = Read-Utf8Json -Path $ManifestPath
    $resolvedArtifacts = @{}
    foreach ($role in @("nsis_installer", "electron_executable", "sidecar")) {
        $matches = @($manifest.artifacts | Where-Object { $_.role -eq $role })
        if ($matches.Count -ne 1) {
            throw [IO.InvalidDataException]::new("Release manifest must contain exactly one $role artifact")
        }
        $artifact = $matches[0]
        $path = Resolve-ReleaseArtifactPath -ReleaseDirectory $ReleaseDirectory -RelativePath ([string]$artifact.path)
        if (-not [IO.File]::Exists($path)) { throw [IO.FileNotFoundException]::new("Release artifact is missing", $path) }
        $actualHash = Get-SmokeFileSha256 -Path $path
        if (-not [string]::Equals($actualHash, [string]$artifact.sha256, [StringComparison]::OrdinalIgnoreCase)) {
            throw [IO.InvalidDataException]::new("Release artifact hash does not match manifest for $role")
        }
        $resolvedArtifacts[$role] = [pscustomobject]@{ Path = $path; Sha256 = $actualHash }
    }
    return [pscustomobject]@{
        Version = [string]$manifest.version
        ReleaseMode = [string]$manifest.release_mode
        ManifestSha256 = Get-SmokeFileSha256 -Path $ManifestPath
        InstallerPath = $resolvedArtifacts.nsis_installer.Path
        InstallerSha256 = $resolvedArtifacts.nsis_installer.Sha256
        ElectronPath = $resolvedArtifacts.electron_executable.Path
        ElectronSha256 = $resolvedArtifacts.electron_executable.Sha256
        SidecarPath = $resolvedArtifacts.sidecar.Path
        SidecarSha256 = $resolvedArtifacts.sidecar.Sha256
    }
}

function Write-WindowsSmokeEvidenceAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Evidence
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetDirectoryName($resolved)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    if ([IO.File]::Exists($resolved)) {
        throw [IO.IOException]::new("Smoke evidence destination must be invalidated before writing")
    }
    $temporary = "$resolved.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Evidence | ConvertTo-Json -Depth 8
        [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $resolved)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Test-WindowsSmokeEvidenceCurrent {
    param(
        [Parameter(Mandatory = $true)][string]$EvidencePath,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ReleaseDirectory
    )
    if (-not [IO.File]::Exists([IO.Path]::GetFullPath($EvidencePath))) { return $false }
    $evidence = Read-Utf8Json -Path $EvidencePath
    if ($evidence.ok -ne $true) { return $false }
    if ([int]$evidence.schema_version -ne 2) { return $false }
    $runId = [Guid]::Empty
    if (-not [Guid]::TryParseExact([string]$evidence.run_id, "N", [ref]$runId)) { return $false }
    $generatedValue = $evidence.generated_at_utc
    $generatedAt = [DateTimeOffset]::MinValue
    if ($generatedValue -is [DateTime]) {
        if ($generatedValue.Kind -ne [DateTimeKind]::Utc) { return $false }
        $generatedAt = [DateTimeOffset]::new($generatedValue)
    } elseif ($generatedValue -is [DateTimeOffset]) {
        $generatedAt = $generatedValue
    } elseif (-not [DateTimeOffset]::TryParse(
        [string]$generatedValue,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$generatedAt
    )) {
        return $false
    }
    if ($generatedAt.Offset -ne [TimeSpan]::Zero) { return $false }
    $binding = Get-WindowsReleaseBinding -ManifestPath $ManifestPath -ReleaseDirectory $ReleaseDirectory
    return (
        [string]::Equals([string]$evidence.version, $binding.Version, [StringComparison]::Ordinal) -and
        [string]::Equals([string]$evidence.release_mode, $binding.ReleaseMode, [StringComparison]::Ordinal) -and
        [string]::Equals([string]$evidence.release_manifest_sha256, $binding.ManifestSha256, [StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$evidence.installer_sha256, $binding.InstallerSha256, [StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$evidence.installed_electron_sha256, $binding.ElectronSha256, [StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$evidence.installed_sidecar_sha256, $binding.SidecarSha256, [StringComparison]::OrdinalIgnoreCase)
    )
}
