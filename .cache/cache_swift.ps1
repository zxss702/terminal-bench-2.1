# Download and cache official Swift Linux release tarballs for TB2 common-env.
# Platforms cover Terminal-Bench bases: bookworm→debian12, noble→ubuntu24.04,
# bullseye→debian11, plus ubuntu22.04 / debian13 when published.
# Cache dir: .cache/swift/swift-<ver>-RELEASE-<platform>.tar.gz
# Containers / common-env build read them from /swift-packages.

param(
    [string]$Version = "6.3.3",
    [string[]]$Platforms = @("debian12", "ubuntu24.04", "debian11", "ubuntu22.04", "debian13")
)

$ErrorActionPreference = "Stop"
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

if ($env:SWIFT_VERSION -and -not $PSBoundParameters.ContainsKey("Version")) {
    $Version = $env:SWIFT_VERSION.Trim()
}
if (-not $Version) { $Version = "6.3.3" }

# Allow -Platforms debian12,ubuntu24.04 (single string) as well as real arrays.
if ($Platforms.Count -eq 1 -and $Platforms[0] -match ",") {
    $Platforms = @($Platforms[0] -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$CacheDir = if ($env:SWIFT_CACHE_DIR) { $env:SWIFT_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\swift" }
$Proxy = if ($env:TB2_PROXY) { $env:TB2_PROXY.Trim() }
          elseif ($env:SWE_BENCH_PROXY) { $env:SWE_BENCH_PROXY.Trim() }
          elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY.Trim() }
          elseif ($env:http_proxy) { $env:http_proxy.Trim() }
          else { "http://127.0.0.1:7897" }

$Branch = "swift-$Version-release"
$SwiftVer = "swift-$Version-RELEASE"
$WebRoot = "https://download.swift.org"

function Invoke-CurlDownload {
    param([string]$Url, [string]$OutFile)
    $curlArgs = @(
        "-L", "--fail", "--retry", "8", "--retry-delay", "3",
        "--connect-timeout", "30", "--max-time", "1800",
        "-o", $OutFile, $Url
    )
    if ($Proxy) { $curlArgs = @("-x", $Proxy) + $curlArgs }
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "Download failed (exit $LASTEXITCODE): $Url" }
}

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
Write-Host "Swift cache"
Write-Host "  Version:   $Version"
Write-Host "  Platforms: $($Platforms -join ', ')"
Write-Host "  Cache:     $CacheDir"
Write-Host "  Proxy:     $Proxy"

$ok = 0
$skip = 0
$fail = 0

foreach ($plat in $Platforms) {
    # URL dir strips dots: ubuntu24.04 → ubuntu2404, debian12 → debian12
    $platDir = $plat -replace '\.', ''
    $asset = "$SwiftVer-$plat.tar.gz"
    $url = "$WebRoot/$Branch/$platDir/$SwiftVer/$asset"
    $dest = Join-Path $CacheDir $asset
    $marker = Join-Path $CacheDir ".$asset.ok"

    if ((Test-Path $dest) -and (Test-Path $marker) -and ((Get-Item $dest).Length -gt 50MB)) {
        Write-Host "[SKIP] $asset already cached ($([math]::Round((Get-Item $dest).Length/1MB)) MB)"
        $skip++
        continue
    }

    Write-Host "[GET] $url"
    $tmp = Join-Path $env:TEMP ("tb2-swift-" + [guid]::NewGuid().ToString() + ".tar.gz")
    try {
        try {
            Invoke-CurlDownload -Url $url -OutFile $tmp
        } catch {
            Write-Host "[WARN] not available for platform '$plat' (skip): $($_.Exception.Message)"
            $fail++
            continue
        }
        $len = (Get-Item $tmp).Length
        if ($len -lt 50MB) { throw "download too small ($len bytes) for $asset" }
        Copy-Item -Force $tmp $dest
        "url=$url`nsize=$len`n" | Set-Content -Path $marker -Encoding utf8
        Write-Host "[OK] $dest ($([math]::Round($len/1MB)) MB)"
        $ok++
    } finally {
        if (Test-Path $tmp) { Remove-Item -Force $tmp }
    }
}

Write-Host ""
Write-Host "Done: downloaded=$ok skipped=$skip unavailable=$fail"
Write-Host "Required for most TB2 tasks: debian12 + ubuntu24.04"
if (-not (Test-Path (Join-Path $CacheDir "$SwiftVer-debian12.tar.gz"))) {
    Write-Host "[WARN] missing debian12 tarball (bookworm / python:*-slim-bookworm)" -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $CacheDir "$SwiftVer-ubuntu24.04.tar.gz"))) {
    Write-Host "[WARN] missing ubuntu24.04 tarball (ubuntu:24.04)" -ForegroundColor Yellow
}
