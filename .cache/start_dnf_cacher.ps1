# Start Squid as the apt/dnf caching forward proxy for TB2 builds.
# Cache dir: .cache/dnf-cacher
# Clients: TB2_PKG_PROXY=http://host.docker.internal:3144 (apt.conf / dnf.conf).
# pip uses Clash + host .cache/pip, not this proxy.
# Upstream: Clash via squid cache_peer on Docker host-gateway IPv4:7897
# This script lives under .cache/; project root is its parent.
#
# Image strategy: build tb2-dnf-cacher:local from python:3.12-slim + apt squid
# (docker.io/ubuntu/squid often fails to pull behind flaky registry auth).
#
# Container name kept as tb2-dnf-cacher for compatibility.

$ErrorActionPreference = "Stop"
# Docker prints progress / "Unable to find image locally" on stderr.
try { $PSNativeCommandUseErrorActionPreference = $false } catch { }
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

$ContainerName = "tb2-dnf-cacher"
$Port = if ($env:TB2_DNF_CACHER_PORT) { $env:TB2_DNF_CACHER_PORT } else { "3144" }
$CacheDir = Join-Path $Root ".cache\dnf-cacher"
$SpoolDir = Join-Path $CacheDir "spool"
$ConfTemplate = Join-Path $CacheDir "squid.conf"
$ConfRuntime = Join-Path $CacheDir "squid.runtime.conf"
$DockerFile = Join-Path $CacheDir "Dockerfile"
$Image = "tb2-dnf-cacher:local"

New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $SpoolDir | Out-Null

if (-not (Test-Path $ConfTemplate)) {
    Write-Host "[ERROR] Missing $ConfTemplate — restore .cache/dnf-cacher/squid.conf first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $DockerFile)) {
    Write-Host "[ERROR] Missing $DockerFile" -ForegroundColor Red
    exit 1
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Docker is not available. Start Docker Desktop first." -ForegroundColor Red
    exit 1
}

# docker pull / build FROM python:3.12-slim must use Clash, never Squid.
if (-not $env:TB2_CLASH_PROXY) { $env:TB2_CLASH_PROXY = "http://127.0.0.1:7897" }
if (-not $env:HTTP_PROXY) { $env:HTTP_PROXY = $env:TB2_CLASH_PROXY }
if (-not $env:HTTPS_PROXY) { $env:HTTPS_PROXY = $env:TB2_CLASH_PROXY }
if (-not $env:http_proxy) { $env:http_proxy = $env:TB2_CLASH_PROXY }
if (-not $env:https_proxy) { $env:https_proxy = $env:TB2_CLASH_PROXY }

$running = docker ps -q -f "name=^/${ContainerName}$"
if ($running) {
    Write-Host "[OK] $ContainerName is already running (port $Port)"
    Write-Host "     In containers: http://host.docker.internal:$Port"
    Write-Host "     Cache dir:     $CacheDir"
    exit 0
}

$existing = docker ps -aq -f "name=^/${ContainerName}$"
if ($existing) {
    Write-Host "Removing stopped container $ContainerName ..."
    docker rm -f $ContainerName | Out-Null
}

function Resolve-UpstreamProxy {
    if ($env:TB2_CONTAINER_PROXY) { return $env:TB2_CONTAINER_PROXY.Trim() }
    if ($env:SWE_BENCH_CONTAINER_PROXY) { return $env:SWE_BENCH_CONTAINER_PROXY.Trim() }
    if ($env:TB2_PROXY) { return $env:TB2_PROXY.Trim() }
    if ($env:SWE_BENCH_PROXY) { return $env:SWE_BENCH_PROXY.Trim() }
    return "http://host.docker.internal:7897"
}

function Resolve-HostGatewayIPv4 {
    # Prefer docker's host-gateway IPv4 (Docker Desktop: usually 192.168.65.254).
    # docker writes "Unable to find image locally" to stderr; with
    # $ErrorActionPreference=Stop that aborts before the command finishes.
    if ($env:TB2_HOST_GATEWAY_IPV4) { return $env:TB2_HOST_GATEWAY_IPV4.Trim() }
    $out = cmd /c "docker run --rm --platform linux/amd64 --add-host=host.docker.internal:host-gateway python:3.12-slim getent ahostsv4 host.docker.internal 2>nul"
    if ($LASTEXITCODE -eq 0 -and $out) {
        $line = ($out | Select-Object -First 1).ToString().Trim()
        if ($line -match '^(\d+\.\d+\.\d+\.\d+)\b') { return $Matches[1] }
    }
    return "192.168.65.254"
}

$upstreamProxy = Resolve-UpstreamProxy
$hostGw = Resolve-HostGatewayIPv4
Write-Host "Host gateway IPv4 (Clash parent): $hostGw"

$template = Get-Content -Raw -Path $ConfTemplate
$runtime = $template.Replace("__HOST_GATEWAY_IPV4__", $hostGw)
# UTF-8 without BOM (Squid is picky about conf encoding on some builds).
[System.IO.File]::WriteAllText($ConfRuntime, $runtime)

$needBuild = $true
$localImg = docker images -q $Image
if ($localImg) {
    Write-Host "Using existing image $Image"
    $needBuild = $false
}

if ($needBuild) {
    Write-Host "Building $Image (python:3.12-slim + squid) ..."
    Write-Host "  Build proxy: $upstreamProxy"
    $buildArgs = @(
        "build",
        "-t", $Image,
        "-f", $DockerFile,
        "--add-host=host.docker.internal:host-gateway",
        "--build-arg", "HTTP_PROXY=$upstreamProxy",
        "--build-arg", "HTTPS_PROXY=$upstreamProxy",
        $CacheDir
    )
    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] docker build of $Image failed." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Starting dnf-cacher (Squid) ..."
Write-Host "  Image: $Image"
Write-Host "  Port:  $Port"
Write-Host "  Cache: $CacheDir"
Write-Host "  Upstream parent: ${hostGw}:7897 (Clash)"

$dockerArgs = @(
    "run", "-d",
    "--name", $ContainerName,
    "--restart", "unless-stopped",
    "-p", "${Port}:3128",
    "-v", "${ConfRuntime}:/etc/squid/squid.conf:ro",
    "-v", "${SpoolDir}:/var/spool/squid",
    "--add-host=host.docker.internal:host-gateway",
    "-e", "HTTP_PROXY=$upstreamProxy",
    "-e", "HTTPS_PROXY=$upstreamProxy",
    "-e", "http_proxy=$upstreamProxy",
    "-e", "https_proxy=$upstreamProxy",
    "-e", "NO_PROXY=localhost,127.0.0.1,host.docker.internal",
    $Image
)

& docker @dockerArgs | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] docker run failed." -ForegroundColor Red
    docker logs $ContainerName 2>&1
    exit 1
}

$probeOk = $false
for ($i = 1; $i -le 90; $i++) {
    $stillRunning = docker ps -q -f "name=^/${ContainerName}$"
    if (-not $stillRunning) {
        Write-Host "[ERROR] Container exited early. Logs:" -ForegroundColor Red
        cmd /c "docker logs $ContainerName 2>&1"
        exit 1
    }
    # docker logs writes to stderr; don't let PS ErrorActionPreference treat it as fatal.
    $logs = cmd /c "docker logs $ContainerName 2>&1"
    $logsText = if ($null -eq $logs) { "" } else { ($logs | Out-String) }
    $accepting = $logsText -match 'Process Roles: master worker'
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect("127.0.0.1", [int]$Port)
        $client.Close()
        if ($accepting) {
            $probeOk = $true
            break
        }
    } catch {
        # still starting
    }
    Start-Sleep -Seconds 2
}

if ($probeOk) {
    Write-Host ""
    Write-Host "[OK] Squid pkg cache is ready (apt/dnf via TB2_PKG_PROXY)." -ForegroundColor Green
    Write-Host "  Host:       http://127.0.0.1:$Port"
    Write-Host "  Containers: TB2_PKG_PROXY=http://host.docker.internal:$Port"
    Write-Host "  Stop:       docker stop $ContainerName"
} else {
    Write-Host "[WARN] Container is running but readiness is unclear." -ForegroundColor Yellow
    Write-Host "       docker logs $ContainerName"
}
