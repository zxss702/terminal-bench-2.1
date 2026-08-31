# Formal-run preflight: Docker, Clash, Squid :3144, pip cache, Claude/agent caches.
# No API calls. Exit 0 if ready.

$ErrorActionPreference = "Continue"
$CacheRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $CacheRoot
Set-Location $Root

$failed = 0
function Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Bad($msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    $script:failed++
}
function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }

function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 2000) {
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $tcp.BeginConnect($HostName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        return $tcp.Connected
    } catch {
        return $false
    } finally {
        $tcp.Close()
    }
}

$Proxy = if ($env:TB2_PROXY) { $env:TB2_PROXY.Trim() }
          elseif ($env:SWE_BENCH_PROXY) { $env:SWE_BENCH_PROXY.Trim() }
          elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY.Trim() }
          elseif ($env:http_proxy) { $env:http_proxy.Trim() }
          else { "http://127.0.0.1:7897" }

Info "project: $Root"
Info "proxy: $Proxy"

& docker info 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "Docker available" }
else { Bad "Docker unavailable - start Docker Desktop" }

try {
    $uri = [Uri]$Proxy
    $hostName = $uri.Host
    $port = if ($uri.Port -gt 0) { $uri.Port } else { 7897 }
    if (Test-Tcp $hostName $port) { Ok "Proxy TCP ${hostName}:${port}" }
    else { Bad "Proxy not reachable at ${hostName}:${port} (start Clash)" }
} catch {
    Bad "Proxy check failed: $_"
}

if (Test-Tcp "127.0.0.1" 3144) { Ok "Squid pkg cache on :3144 (apt/dnf)" }
else { Bad "Squid not on :3144 - run .\.cache\start_dnf_cacher.ps1" }

$ClaudeCache = if ($env:CLAUDE_CACHE_DIR) { $env:CLAUDE_CACHE_DIR.Trim() }
               else { Join-Path $Root ".cache\claude-code" }
if (Test-Path -LiteralPath $ClaudeCache -PathType Leaf) {
    Bad "Claude cache is a FILE (not dir): $ClaudeCache - delete it, run .\.cache\cache_claude.ps1"
} elseif (-not (Test-Path -LiteralPath $ClaudeCache -PathType Container)) {
    Bad "Claude cache missing: $ClaudeCache - run .\.cache\cache_claude.ps1"
} else {
    $bin = Join-Path $ClaudeCache "linux-x64\claude"
    if (Test-Path -LiteralPath $bin -PathType Leaf) { Ok "Claude binary: $bin" }
    else { Bad "Missing Claude binary: $bin - run .\.cache\cache_claude.ps1" }
}

$PipCache = if ($env:TB2_PIP_CACHE_DIR) { $env:TB2_PIP_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\pip" }
$pipReady = $false
if (Test-Path -LiteralPath $PipCache -PathType Container) {
    foreach ($sub in @("http", "http-v2", "wheels")) {
        if (Test-Path -LiteralPath (Join-Path $PipCache $sub) -PathType Container) {
            $pipReady = $true
            break
        }
    }
}
if ($pipReady) { Ok "pip cache: $PipCache" }
else { Bad "pip cache not prewarmed: $PipCache - run .\.cache\cache_agent_packages.ps1" }

$AfDir = Join-Path $Root ".cache\agents\agentflow"
$afReady = (Test-Path (Join-Path $AfDir "pyproject.toml")) -or (Test-Path (Join-Path $AfDir "agentflow\pyproject.toml"))
if ($afReady) { Ok "AgentFlow source: $AfDir" }
else { Bad "AgentFlow source missing: $AfDir - run .\.cache\cache_agent_packages.ps1" }

$UvVer = if ($env:TB2_UV_VERSION) { $env:TB2_UV_VERSION.Trim() } else { "0.9.5" }
$UvDir = if ($env:TB2_UV_CACHE_DIR) { $env:TB2_UV_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\uv" }
$UvBin = Join-Path $UvDir "$UvVer\uv"
$UvxBin = Join-Path $UvDir "$UvVer\uvx"
if ((Test-Path -LiteralPath $UvBin -PathType Leaf) -and (Test-Path -LiteralPath $UvxBin -PathType Leaf)) {
    Ok "uv cache $UvVer : $UvBin"
} else {
    Bad "uv cache missing ($UvVer) - run .\.cache\cache_uv.ps1"
}

$SwiftDir = if ($env:SWIFT_CACHE_DIR) { $env:SWIFT_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\swift" }
if (Test-Path -LiteralPath $SwiftDir) {
    Info "swift cache present but unused (browser/Swift disabled): $SwiftDir"
} else {
    Ok "swift cache not required (browser/Swift disabled)"
}

foreach ($f in @("syAgentInfo.json", "claude_settings.json")) {
    $p = Join-Path $Root $f
    if (Test-Path -LiteralPath $p -PathType Leaf) { Ok $f }
    else { Bad "Missing $f" }
}

$settings = Get-Content (Join-Path $Root "claude_settings.json") -Raw -ErrorAction SilentlyContinue
if ($settings -match "CLAUDE_CODE_MAX_OUTPUT_TOKENS") { Ok "CLAUDE_CODE_MAX_OUTPUT_TOKENS set" }
else { Bad "claude_settings.json missing CLAUDE_CODE_MAX_OUTPUT_TOKENS" }

Write-Host ""
if ($failed -gt 0) {
    Write-Host "Preflight FAILED ($failed). Fix before formal run." -ForegroundColor Red
    exit 1
}
Write-Host "Preflight PASSED. Archive/clear traj/ yourself, then run the bench." -ForegroundColor Green
exit 0
