# Download and cache Astral uv/uvx Linux binaries for TB verify (test.sh).
# All bench/*/tests/test.sh pin: curl -LsSf https://astral.sh/uv/0.9.5/install.sh
# Cache dir: .cache/uv/<version>/{uv,uvx}
# Containers mount this as /tb2-uv-cache (read-only); harness seeds ~/.local/bin
# before bash /tests/test.sh so GitHub SSL flakes do not zero the reward.

param(
    [string]$Version = "0.9.5"
)

$ErrorActionPreference = "Stop"
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

$CacheDir = if ($env:TB2_UV_CACHE_DIR) { $env:TB2_UV_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\uv" }
$Proxy = if ($env:TB2_PROXY) { $env:TB2_PROXY.Trim() }
          elseif ($env:SWE_BENCH_PROXY) { $env:SWE_BENCH_PROXY.Trim() }
          elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY.Trim() }
          elseif ($env:http_proxy) { $env:http_proxy.Trim() }
          else { "http://127.0.0.1:7897" }

if ($env:TB2_UV_VERSION -and -not $PSBoundParameters.ContainsKey("Version")) {
    $Version = $env:TB2_UV_VERSION.Trim()
}
if (-not $Version) { $Version = "0.9.5" }

$Arch = "x86_64-unknown-linux-gnu"
$Asset = "uv-$Arch.tar.gz"
$Url = "https://github.com/astral-sh/uv/releases/download/$Version/$Asset"

$DestDir = Join-Path $CacheDir $Version
$Tarball = Join-Path $DestDir $Asset
$UvBin = Join-Path $DestDir "uv"
$UvxBin = Join-Path $DestDir "uvx"
$OkMarker = Join-Path $DestDir ".tb2-ok"

function Invoke-CurlDownload {
    param([string]$Url, [string]$OutFile)
    $curlArgs = @(
        "-L", "--fail", "--retry", "8", "--retry-delay", "2",
        "--connect-timeout", "30", "--max-time", "600",
        "-o", $OutFile, $Url
    )
    if ($Proxy) { $curlArgs = @("-x", $Proxy) + $curlArgs }
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "Download failed (exit $LASTEXITCODE): $Url" }
}

Write-Host "uv cache"
Write-Host "  Version: $Version"
Write-Host "  Asset:   $Asset"
Write-Host "  Cache:   $DestDir"
Write-Host "  Proxy:   $Proxy"

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

if ((Test-Path $UvBin) -and (Test-Path $UvxBin) -and (Test-Path $OkMarker)) {
    Write-Host "Already cached: $UvBin"
    Write-Host "Skip download (delete $OkMarker to force refresh)."
    exit 0
}

$tmp = Join-Path $env:TEMP ("tb2-uv-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $tmpTar = Join-Path $tmp $Asset
    Write-Host "Downloading $Url ..."
    Invoke-CurlDownload -Url $Url -OutFile $tmpTar
    Copy-Item -Force $tmpTar $Tarball

    Write-Host "Extracting..."
    # tar.exe is available on modern Windows
    & tar.exe -xzf $tmpTar -C $tmp
    if ($LASTEXITCODE -ne 0) { throw "tar extract failed" }

    $foundUv = Get-ChildItem -Path $tmp -Recurse -Filter "uv" -File | Select-Object -First 1
    $foundUvx = Get-ChildItem -Path $tmp -Recurse -Filter "uvx" -File | Select-Object -First 1
    if (-not $foundUv -or -not $foundUvx) {
        throw "uv/uvx not found inside tarball"
    }
    Copy-Item -Force $foundUv.FullName $UvBin
    Copy-Item -Force $foundUvx.FullName $UvxBin
    "version=$Version`nasset=$Asset`nurl=$Url`n" | Set-Content -Path $OkMarker -Encoding utf8
    Write-Host "OK: $UvBin"
    Write-Host "OK: $UvxBin"
}
finally {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
}
