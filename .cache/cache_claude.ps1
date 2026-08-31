# Download and cache Claude Code Linux binaries for SWE containers.
# Caches both glibc (linux-x64) and musl (linux-x64-musl) for Alpine images.
# Cache dir: .cache/claude-code/
# Containers mount this dir as /claude-packages (read-only).

param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

$CacheDir = if ($env:CLAUDE_CACHE_DIR) { $env:CLAUDE_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\claude-code" }
$Proxy = if ($env:SWE_BENCH_PROXY) { $env:SWE_BENCH_PROXY.Trim() }
          elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY.Trim() }
          elseif ($env:http_proxy) { $env:http_proxy.Trim() }
          else { "http://127.0.0.1:7897" }

$GcsBase = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"
$DownloadBase = "https://downloads.claude.ai/claude-code-releases"
# Alpine SWE images need musl; Debian/Ubuntu need glibc.
$Platforms = @("linux-x64", "linux-x64-musl")

function Invoke-CurlDownload {
    param(
        [string]$Url,
        [string]$OutFile
    )
    $curlArgs = @(
        "-L", "--fail", "--retry", "5", "--retry-delay", "2",
        "-o", $OutFile, $Url
    )
    if ($Proxy) {
        $curlArgs = @("-x", $Proxy) + $curlArgs
    }
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed: $Url"
    }
}

function Get-CurlText {
    param([string]$Url)
    $tmp = Join-Path $env:TEMP ("claude-cache-" + [guid]::NewGuid().ToString() + ".txt")
    try {
        Invoke-CurlDownload -Url $Url -OutFile $tmp
        return (Get-Content -Raw -Path $tmp).Trim()
    }
    finally {
        if (Test-Path $tmp) { Remove-Item -Force $tmp }
    }
}

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

if (-not $Version) {
    if ($env:CLAUDE_VERSION) {
        $Version = $env:CLAUDE_VERSION.Trim()
    }
}
if (-not $Version) {
    Write-Host "Resolving latest Claude Code version..."
    $Version = Get-CurlText -Url "$GcsBase/latest"
}

Write-Host "Claude Code cache"
Write-Host "  Version:   $Version"
Write-Host "  Platforms: $($Platforms -join ', ')"
Write-Host "  Cache:     $CacheDir"
Write-Host "  Proxy:     $Proxy"

$destDir = Join-Path $CacheDir $Version
$destManifest = Join-Path $destDir "manifest.json"
$latestPointer = Join-Path $CacheDir "latest"

New-Item -ItemType Directory -Force -Path $destDir | Out-Null

$manifestUrl = "$DownloadBase/$Version/manifest.json"
if (-not (Test-Path $destManifest)) {
    Write-Host "Downloading manifest..."
    Write-Host "  URL: $manifestUrl"
    $tmpManifest = "$destManifest.partial"
    if (Test-Path $tmpManifest) { Remove-Item -Force $tmpManifest }
    Invoke-CurlDownload -Url $manifestUrl -OutFile $tmpManifest
    Move-Item -Force $tmpManifest $destManifest
}
else {
    Write-Host "[OK] Already cached: $Version/manifest.json"
}

foreach ($platform in $Platforms) {
    $platformDir = Join-Path $destDir $platform
    $destBinary = Join-Path $platformDir "claude"
    $flatDir = Join-Path $CacheDir $platform
    $flatBinary = Join-Path $flatDir "claude"

    New-Item -ItemType Directory -Force -Path $platformDir | Out-Null
    New-Item -ItemType Directory -Force -Path $flatDir | Out-Null

    if ((Test-Path $destBinary) -and ((Get-Item $destBinary).Length -gt 1MB)) {
        $sizeMb = [math]::Round((Get-Item $destBinary).Length / 1MB, 1)
        Write-Host "[OK] Already cached: $Version/$platform/claude ($sizeMb MB)"
    }
    else {
        $binaryUrl = "$DownloadBase/$Version/$platform/claude"
        Write-Host "Downloading $platform binary..."
        Write-Host "  URL: $binaryUrl"
        $tmpBinary = "$destBinary.partial"
        if (Test-Path $tmpBinary) { Remove-Item -Force $tmpBinary }
        Invoke-CurlDownload -Url $binaryUrl -OutFile $tmpBinary
        Move-Item -Force $tmpBinary $destBinary

        $sizeMb = [math]::Round((Get-Item $destBinary).Length / 1MB, 1)
        Write-Host "[OK] Cached: $Version/$platform/claude ($sizeMb MB)"
    }

    # Flat layout used by env_install.sh: /claude-packages/<platform>/claude
    Copy-Item -Force $destBinary $flatBinary
}

# Legacy flat path (glibc) for older scripts
$legacyFlat = Join-Path $CacheDir "claude"
Copy-Item -Force (Join-Path $CacheDir "linux-x64\claude") $legacyFlat
Set-Content -Path $latestPointer -Value $Version -NoNewline

Write-Host ""
Write-Host "Done. Containers will mount: $CacheDir -> /claude-packages"
Write-Host "  glibc:  $CacheDir\linux-x64\claude"
Write-Host "  musl:   $CacheDir\linux-x64-musl\claude"
Write-Host "Install with: .\cache_claude.ps1"
Write-Host "              .\cache_claude.ps1 -Version 2.1.218"
Write-Host "Note: Alpine images need the musl build. Re-run this script if you only cached glibc before."

