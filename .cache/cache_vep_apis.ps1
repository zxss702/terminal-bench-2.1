# Cache Ensembl/VEP Perl APIs (Bio/) so docker builds skip INSTALL.pl --AUTO a.
# INSTALL.pl hits api.github.com and often 403s; this tarball is unpacked instead.
#
# Cache dir: .cache/vep-apis/<release>/ensembl-apis.tar.gz
#
# Sources (first that works):
#   1) -FromImage <tag>  — tar Bio/ out of an existing VEP image
#   2) -FromHostPath     — copy a pre-made ensembl-apis.tar.gz
#   3) bootstrap via docker+Clash running INSTALL.pl once, then pack Bio/

param(
    [string]$Release = "115",
    [string]$FromImage = "",
    [string]$FromHostPath = ""
)

$ErrorActionPreference = "Stop"
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

if ($env:TB2_VEP_APIS_RELEASE -and -not $PSBoundParameters.ContainsKey("Release")) {
    $Release = $env:TB2_VEP_APIS_RELEASE.Trim()
}
if (-not $Release) { $Release = "115" }

$CacheDir = if ($env:TB2_VEP_APIS_CACHE_DIR) { $env:TB2_VEP_APIS_CACHE_DIR.Trim() }
            else { Join-Path $Root ".cache\vep-apis" }
$DestDir = Join-Path $CacheDir $Release
$Tarball = Join-Path $DestDir "ensembl-apis.tar.gz"
$OkMarker = Join-Path $DestDir ".tb2-ok"

$Proxy = if ($env:TB2_PROXY) { $env:TB2_PROXY.Trim() }
          elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY.Trim() }
          elseif ($env:http_proxy) { $env:http_proxy.Trim() }
          else { "http://127.0.0.1:7897" }

Write-Host "vep-apis cache"
Write-Host "  Release: $Release"
Write-Host "  Cache:   $DestDir"
Write-Host "  Proxy:   $Proxy"

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

if ((Test-Path $Tarball) -and (Test-Path $OkMarker) -and ((Get-Item $Tarball).Length -gt 100000)) {
    Write-Host "Already cached: $Tarball ($((Get-Item $Tarball).Length) bytes)"
    Write-Host "Skip (delete $OkMarker to force refresh)."
    exit 0
}

function Write-OkMarker([string]$Source) {
    @"
release=$Release
source=$Source
"@ | Set-Content -Path $OkMarker -Encoding utf8
}

if ($FromHostPath) {
    if (-not (Test-Path $FromHostPath)) { throw "Not found: $FromHostPath" }
    Copy-Item -Force $FromHostPath $Tarball
    Write-OkMarker "host:$FromHostPath"
    Write-Host "OK: $Tarball"
    exit 0
}

$packScript = @"
set -euo pipefail
VEP=/app/data/ensembl-vep-release-$Release
if [ ! -d "`$VEP/Bio" ]; then
  echo "Bio/ missing under `$VEP" >&2
  exit 1
fi
tar -czf /out/ensembl-apis.tar.gz -C "`$VEP" Bio
ls -la /out/ensembl-apis.tar.gz
test -d /tmp || true
tar -tzf /out/ensembl-apis.tar.gz | head -5
"@
$packPath = Join-Path $DestDir "_pack.sh"
# LF-only for bash
[System.IO.File]::WriteAllText($packPath, ($packScript -replace "`r`n", "`n" -replace "`r", "`n"))

try {
    $image = $FromImage
    if (-not $image) {
        # Prefer known local tags that already ran INSTALL.pl
        $candidates = @(
            "tb3-local/atrx-vep-crispr:verifier-manual",
            "tb3-local/atrx-vep-crispr:verifier",
            "tb3-local/atrx-vep-crispr:common-env",
            "tb3-local/atrx-vep-crispr:base"
        )
        foreach ($c in $candidates) {
            docker image inspect $c 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                # Confirm Bio exists
                docker run --rm $c bash -lc "test -d /app/data/ensembl-vep-release-$Release/Bio" 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { $image = $c; break }
            }
        }
    }

    if ($image) {
        Write-Host "Packing Bio/ from image: $image"
        docker run --rm -v "${DestDir}:/out" $image bash /out/_pack.sh
        if ($LASTEXITCODE -ne 0) { throw "pack from image failed" }
        Write-OkMarker "image:$image"
        Write-Host "OK: $Tarball"
        exit 0
    }

    Write-Host "No local VEP image with Bio/; bootstrapping via INSTALL.pl + Clash..."
    $bootScript = @"
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  perl curl ca-certificates tar gzip \
  libdbi-perl libdbd-mysql-perl libbio-perl-perl \
  libset-intervaltree-perl libjson-perl libfile-copy-recursive-perl \
  libtry-tiny-perl libcgi-pm-perl
# Expect host-mounted release tarball at /seed/ensembl-vep-release-$Release.tar.gz
mkdir -p /app/data
tar -xzf /seed/ensembl-vep-release-$Release.tar.gz -C /app/data
cd /app/data/ensembl-vep-release-$Release
perl INSTALL.pl --AUTO a --NO_HTSLIB --NO_TEST --NO_UPDATE
tar -czf /out/ensembl-apis.tar.gz Bio
ls -la /out/ensembl-apis.tar.gz
"@
    $bootPath = Join-Path $DestDir "_boot.sh"
    [System.IO.File]::WriteAllText($bootPath, ($bootScript -replace "`r`n", "`n" -replace "`r", "`n"))

    $seedCandidates = @(
        (Join-Path $Root "tasks\atrx-vep-crispr\tests\data\ensembl-vep-release-$Release.tar.gz"),
        (Join-Path $Root "tasks\atrx-vep-crispr\environment\data\ensembl-vep-release-$Release.tar.gz")
    )
    $seed = $seedCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $seed) {
        throw "Missing ensembl-vep-release-$Release.tar.gz under tasks/atrx-vep-crispr/{tests,environment}/data/"
    }
    $seedDir = Split-Path -Parent $seed

    docker run --rm --add-host=host.docker.internal:host-gateway `
      -e "HTTP_PROXY=http://host.docker.internal:7897" `
      -e "HTTPS_PROXY=http://host.docker.internal:7897" `
      -v "${DestDir}:/out" `
      -v "${seedDir}:/seed:ro" `
      python:3.11-slim bash /out/_boot.sh
    if ($LASTEXITCODE -ne 0) { throw "bootstrap INSTALL.pl failed" }
    Write-OkMarker "bootstrap:INSTALL.pl"
    Write-Host "OK: $Tarball"
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $packPath
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $DestDir "_boot.sh")
}
