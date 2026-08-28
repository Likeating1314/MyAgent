$ErrorActionPreference = "Stop"

$frontendDir = (Resolve-Path "$PSScriptRoot\..").Path
$repoDir = (Resolve-Path "$frontendDir\..").Path
$toolDir = "$repoDir\.tmp\desktop-build-tools"
$electronVersion = "37.10.3"
$electronArchiveName = "electron-v$electronVersion-win32-x64.zip"
$electronSha256 = "af69b8a4326432a8bf655eb23f808dcff02bcb2555cac9667e72e943f53542e4"
$nsisSha256 = "56997fdefe25e7928a1a68b4583d08b240b66cf660234053b20131a74cc082f4"
$sevenZipSha256 = "be071f15bd6da2f78fe81c6ddef2009b0c4d8a51f36b780cb806c7e6df95e1b3"
$releaseDir = "$repoDir\.tmp\electron-release-p2c"
. "$repoDir\scripts\windows_release.ps1"
. "$PSScriptRoot\windows_smoke_evidence.ps1"
Invalidate-WindowsSmokeEvidence -Path "$releaseDir\windows-install-smoke.json"
$signing = Get-WindowsSigningConfiguration

if ($signing.Signed) {
    $env:WINDOWS_SIGNTOOL_PATH = $signing.SignTool
}

function Test-Checksum([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $hash = [System.BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    return $hash -eq $Expected
}

function Get-VerifiedFile([string]$Uri, [string]$Path, [string]$Expected) {
    if (Test-Checksum $Path $Expected) { return }
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Force
    }
    Invoke-WebRequest -Uri $Uri -OutFile $Path
    if (-not (Test-Checksum $Path $Expected)) {
        throw "Checksum verification failed for $Path"
    }
}

New-Item -ItemType Directory -Path $toolDir -Force | Out-Null

Push-Location $frontendDir
try {
    & npm run icons:build
    if ($LASTEXITCODE -ne 0) { throw "Icon build failed" }
    & npm run sidecar:build
    if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed" }
    $rendererStaging = "$repoDir\.tmp\desktop-renderer-dist"
    & npx vue-tsc --noEmit
    if ($LASTEXITCODE -ne 0) { throw "Frontend type check failed" }
    & npx vite build --configLoader native --outDir $rendererStaging --emptyOutDir
    if ($LASTEXITCODE -ne 0) { throw "Frontend staging build failed" }

    $electronArchive = Get-ChildItem -Recurse -File "$env:LOCALAPPDATA\electron\Cache" `
        -Filter $electronArchiveName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $electronArchive -or -not (Test-Checksum $electronArchive.FullName $electronSha256)) {
        $electronArchivePath = "$toolDir\$electronArchiveName"
        Get-VerifiedFile `
            "https://github.com/electron/electron/releases/download/v$electronVersion/$electronArchiveName" `
            $electronArchivePath `
            $electronSha256
    } else {
        $electronArchivePath = $electronArchive.FullName
    }

    $electronDist = "$toolDir\electron-v$electronVersion"
    if (-not (Test-Path -LiteralPath "$electronDist\electron.exe")) {
        if (Test-Path -LiteralPath $electronDist) {
            throw "Incomplete Electron tool directory must be removed manually: $electronDist"
        }
        Expand-Archive -LiteralPath $electronArchivePath -DestinationPath $electronDist
    }

    $nsisArchive = "$toolDir\nsis-bundle-3.12.tar.gz"
    Get-VerifiedFile `
        "https://github.com/electron-userland/electron-builder-binaries/releases/download/nsis%401.2.1/nsis-bundle-3.12.tar.gz" `
        $nsisArchive `
        $nsisSha256
    $nsisDir = "$toolDir\nsis-bundle"
    if (-not (Test-Path -LiteralPath "$nsisDir\makensis.cmd")) {
        New-Item -ItemType Directory -Path $nsisDir -Force | Out-Null
        & tar -xzf $nsisArchive --strip-components=1 -C $nsisDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to extract NSIS tools" }
    }

    $sevenZipArchive = "$toolDir\7zip-win-x64.tar.gz"
    Get-VerifiedFile `
        "https://github.com/electron-userland/electron-builder-binaries/releases/download/7zip%401.0.0/7zip-win-x64.tar.gz" `
        $sevenZipArchive `
        $sevenZipSha256
    $sevenZipDir = "$toolDir\7zip"
    if (-not (Test-Path -LiteralPath "$sevenZipDir\bin\7za.exe")) {
        New-Item -ItemType Directory -Path $sevenZipDir -Force | Out-Null
        & tar -xzf $sevenZipArchive --strip-components=1 -C $sevenZipDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to extract 7zip tools" }
    }

    $env:ELECTRON_BUILDER_NSIS_DIR = $nsisDir
    $env:ELECTRON_BUILDER_NSIS_RESOURCES_DIR = $nsisDir
    $env:ELECTRON_BUILDER_7ZIP_PATH = "$sevenZipDir\bin\7za.exe"
    & npx electron-builder --win nsis --config electron-builder.p2b.cjs --config.electronDist="$electronDist"
    if ($LASTEXITCODE -ne 0) { throw "Electron builder failed" }

    $packageJson = [IO.File]::ReadAllText(
        "$frontendDir\package.json",
        [Text.Encoding]::UTF8
    )
    $package = $packageJson | ConvertFrom-Json
    $installer = Get-ChildItem -LiteralPath $releaseDir -File -Filter "*.exe" |
        Where-Object { $_.Name -like "*Setup*" } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    $desktopExecutable = "$releaseDir\win-unpacked\$($package.build.productName).exe"
    $packagedSidecar = "$releaseDir\win-unpacked\resources\backend\local-agent-backend.exe"
    if ($null -eq $installer) { throw "NSIS installer was not generated" }

    $artifacts = @(
        (Get-WindowsArtifactEvidence -Path $packagedSidecar -Role "sidecar" -BasePath $releaseDir),
        (Get-WindowsArtifactEvidence -Path $desktopExecutable -Role "electron_executable" -BasePath $releaseDir),
        (Get-WindowsArtifactEvidence -Path $installer.FullName -Role "nsis_installer" -BasePath $releaseDir)
    )
    $pythonExe = (Resolve-Path "$repoDir\backend\.venv\Scripts\python.exe").Path
    $tools = [ordered]@{
        python = (& $pythonExe --version 2>&1 | Out-String).Trim()
        pyinstaller = (& $pythonExe -m PyInstaller --version 2>&1 | Out-String).Trim()
        node = (& node --version | Out-String).Trim()
        npm = (& npm --version | Out-String).Trim()
        electron = [string]$package.devDependencies.electron
        electron_builder = [string]$package.devDependencies.'electron-builder'
        nsis = "3.12"
    }
    Write-WindowsReleaseManifest `
        -OutputPath "$releaseDir\release-manifest.json" `
        -Version $package.version `
        -Configuration $signing `
        -Artifacts $artifacts `
        -Tools $tools
} finally {
    Pop-Location
}
