$ErrorActionPreference = "Stop"

function Get-WindowsReleaseMode {
    $mode = $env:WINDOWS_RELEASE_MODE
    if ([string]::IsNullOrWhiteSpace($mode)) {
        return "unsigned-development"
    }
    if ($mode -notin @("unsigned-development", "signed-release")) {
        throw "WINDOWS_RELEASE_MODE must be unsigned-development or signed-release"
    }
    return $mode
}

function Find-WindowsSignTool {
    if (-not [string]::IsNullOrWhiteSpace($env:WINDOWS_SIGNTOOL_PATH)) {
        $configured = (Resolve-Path -LiteralPath $env:WINDOWS_SIGNTOOL_PATH -ErrorAction Stop).Path
        return $configured
    }

    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    $candidate = Get-ChildItem -LiteralPath $kitsRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw "Windows signtool.exe was not found"
    }
    return $candidate
}

function Get-WindowsFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [IO.File]::OpenRead((Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path)
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

function Get-WindowsSigningConfiguration {
    $mode = Get-WindowsReleaseMode
    if ($mode -eq "unsigned-development") {
        return [pscustomobject]@{ Mode = $mode; Signed = $false }
    }

    $requiredNames = @(
        "WINDOWS_SIGN_CERTIFICATE_PATH",
        "WINDOWS_SIGN_CERTIFICATE_PASSWORD",
        "WINDOWS_SIGN_TIMESTAMP_URL",
        "WINDOWS_SIGN_EXPECTED_PUBLISHER"
    )
    foreach ($name in $requiredNames) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Signed release requires $name"
        }
    }

    $certificatePath = (Resolve-Path -LiteralPath $env:WINDOWS_SIGN_CERTIFICATE_PATH -ErrorAction Stop).Path
    $timestampUri = $null
    if (-not [Uri]::TryCreate($env:WINDOWS_SIGN_TIMESTAMP_URL, [UriKind]::Absolute, [ref]$timestampUri) -or
        $timestampUri.Scheme -ne "https") {
        throw "WINDOWS_SIGN_TIMESTAMP_URL must be an absolute HTTPS URL"
    }

    return [pscustomobject]@{
        Mode = $mode
        Signed = $true
        CertificatePath = $certificatePath
        CertificatePassword = $env:WINDOWS_SIGN_CERTIFICATE_PASSWORD
        TimestampUrl = $env:WINDOWS_SIGN_TIMESTAMP_URL
        ExpectedPublisher = $env:WINDOWS_SIGN_EXPECTED_PUBLISHER
        SignTool = Find-WindowsSignTool
    }
}

function Invoke-WindowsAuthenticodeSign {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Configuration
    )
    if (-not $Configuration.Signed) { return }
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    & $Configuration.SignTool sign /fd SHA256 /td SHA256 /tr $Configuration.TimestampUrl `
        /f $Configuration.CertificatePath /p $Configuration.CertificatePassword $resolved
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode signing failed for $(Split-Path -Leaf $resolved)"
    }
}

function Get-WindowsArtifactEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][string]$BasePath
    )
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $securityModule = "$env:WINDIR\System32\WindowsPowerShell\v1.0\Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
    if ($PSVersionTable.PSEdition -eq "Desktop" -and (Test-Path -LiteralPath $securityModule)) {
        Import-Module -Name $securityModule -ErrorAction Stop
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    $signerSubject = $null
    $signerPublisher = $null
    if ($null -ne $signature.SignerCertificate) {
        $signerSubject = $signature.SignerCertificate.Subject
        $signerPublisher = $signature.SignerCertificate.GetNameInfo(
            [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
            $false
        )
    }
    $timestampSubject = $null
    if ($null -ne $signature.TimeStamperCertificate) {
        $timestampSubject = $signature.TimeStamperCertificate.Subject
    }
    $resolvedBase = (Resolve-Path -LiteralPath $BasePath -ErrorAction Stop).Path.TrimEnd("\") + "\"
    $baseUri = [Uri]::new($resolvedBase)
    $relativePath = [Uri]::UnescapeDataString($baseUri.MakeRelativeUri([Uri]::new($resolved)).ToString())
    $item = Get-Item -LiteralPath $resolved
    return [ordered]@{
        role = $Role
        path = $relativePath
        size = $item.Length
        sha256 = Get-WindowsFileSha256 -Path $resolved
        authenticode_status = [string]$signature.Status
        signer_subject = $signerSubject
        signer_publisher = $signerPublisher
        timestamp_present = $null -ne $signature.TimeStamperCertificate
        timestamp_subject = $timestampSubject
    }
}

function Assert-WindowsArtifactSignature {
    param(
        [Parameter(Mandatory = $true)]$Evidence,
        [Parameter(Mandatory = $true)]$Configuration
    )
    if (-not $Configuration.Signed) { return }
    if ($Evidence.authenticode_status -ne "Valid") {
        throw "Signature verification failed for $($Evidence.role): $($Evidence.authenticode_status)"
    }
    if ($Evidence.signer_publisher -ne $Configuration.ExpectedPublisher) {
        throw "Publisher verification failed for $($Evidence.role)"
    }
    if (-not $Evidence.timestamp_present) {
        throw "Timestamp verification failed for $($Evidence.role)"
    }
}

function Write-WindowsReleaseManifest {
    param(
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)]$Configuration,
        [Parameter(Mandatory = $true)][array]$Artifacts,
        [Parameter(Mandatory = $true)]$Tools
    )
    foreach ($artifact in $Artifacts) {
        Assert-WindowsArtifactSignature -Evidence $artifact -Configuration $Configuration
    }
    if ($Configuration.Signed) {
        $publishers = @($Artifacts | ForEach-Object { $_.signer_publisher } | Select-Object -Unique)
        if ($publishers.Count -ne 1) {
            throw "Signed artifacts do not have one consistent publisher"
        }
    }

    $manifest = [ordered]@{
        schema_version = 1
        version = $Version
        platform = "win32"
        arch = "x64"
        release_mode = $Configuration.Mode
        generated_at_utc = [DateTime]::UtcNow.ToString("o")
        artifacts = $Artifacts
        tools = $Tools
    }
    $parent = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
}
