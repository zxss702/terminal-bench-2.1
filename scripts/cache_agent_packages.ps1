# AgentFlow source cache + optional host pip HTTP/wheel cache under .cache/pip.
# Host .cache/pip is only for seeding BuildKit cache id=tb2-pip (not a runtime bind).
# pip talks to official PyPI through Clash. Squid is not used for pip.
# Prewarm skips vllm/easyocr/transformers (CUDA torch, hundreds of MB, no TTY progress).

param(
    [string[]]$PythonTags = @("3.11-slim", "3.12-slim", "3.13-slim")
)

$ErrorActionPreference = "Stop"
try { $PSNativeCommandUseErrorActionPreference = $false } catch { }
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

$AgentsDir = if ($env:TB2_AGENTS_CACHE_DIR) { $env:TB2_AGENTS_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\agents" }
$PipCacheDir = if ($env:TB2_PIP_CACHE_DIR) { $env:TB2_PIP_CACHE_DIR.Trim() } else { Join-Path $Root ".cache\pip" }
$AfDir = Join-Path $AgentsDir "agentflow"
$OkMarker = Join-Path $AgentsDir ".tb2-ok"
$Clash = if ($env:TB2_CLASH_PROXY) { $env:TB2_CLASH_PROXY.Trim() } else { "http://127.0.0.1:7897" }
$ClashContainer = "http://host.docker.internal:7897"
$AfZipUrl = "https://github.com/lupantech/AgentFlow/archive/refs/heads/main.zip"

function Invoke-CurlDownload {
    param([string]$Url, [string]$OutFile)
    $curlArgs = @(
        "-L", "--fail", "--retry", "8", "--retry-delay", "2",
        "--connect-timeout", "30", "--max-time", "600",
        "-o", $OutFile, $Url
    )
    if ($Clash) { $curlArgs = @("-x", $Clash) + $curlArgs }
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) { throw "Download failed (exit $LASTEXITCODE): $Url" }
}

Write-Host "agent package cache"
Write-Host "  AgentFlow: $AfDir"
Write-Host "  pip cache: $PipCacheDir -> /root/.cache/pip"
Write-Host "  Clash:     $Clash (zip + pip)"
Write-Host "  Images:    $($PythonTags -join ', ')"

New-Item -ItemType Directory -Force -Path $AgentsDir | Out-Null
New-Item -ItemType Directory -Force -Path $PipCacheDir | Out-Null

$wheelsLegacy = Join-Path $AgentsDir "wheels"
if (Test-Path $wheelsLegacy) {
    Write-Host "removing leftover handmade wheels: $wheelsLegacy"
    Remove-Item -Recurse -Force $wheelsLegacy
}

$tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    throw "Clash :7897 is not up. Start Clash, then re-run."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) { throw "docker not on PATH (need Docker Desktop to prewarm linux pip)" }

$needAf = -not (
    (Test-Path (Join-Path $AfDir "pyproject.toml")) -or
    (Test-Path (Join-Path $AfDir "agentflow\pyproject.toml"))
)
if ($needAf) {
    Write-Host "Downloading AgentFlow source zip..."
    $tmp = Join-Path $env:TEMP ("tb2-agentflow-" + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "agentflow.zip"
        Invoke-CurlDownload -Url $AfZipUrl -OutFile $zip
        & tar.exe -xf $zip -C $tmp
        if ($LASTEXITCODE -ne 0) { throw "tar extract AgentFlow zip failed" }
        $inner = Get-ChildItem -Path $tmp -Directory | Where-Object { $_.Name -like "AgentFlow*" } | Select-Object -First 1
        if (-not $inner) { throw "AgentFlow-main directory missing in zip" }
        if (Test-Path $AfDir) { Remove-Item -Recurse -Force $AfDir }
        Copy-Item -Recurse -Force $inner.FullName $AfDir
        Write-Host "[OK] AgentFlow source -> $AfDir"
    }
    finally {
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    }
}
else {
    Write-Host "[OK] AgentFlow source already cached: $AfDir"
}

$req = Join-Path $AfDir "agentflow\requirements.txt"
if (-not (Test-Path $req)) {
    throw "AgentFlow requirements.txt missing: $req"
}

function Invoke-PipPrewarm {
    param([string]$Image, [string]$InnerCmd)
    # Write a LF-only script and mount it. Piping into docker.exe on Windows
    # re-inserts CR and bash dies with $'\r': command not found.
    $unix = (($InnerCmd -replace "`r`n", "`n") -replace "`r", "").Trim() + "`n"
    $sh = Join-Path $env:TEMP ("tb2-prewarm-" + [guid]::NewGuid().ToString() + ".sh")
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($sh, $unix, $utf8)
    Write-Host "pip prewarm (Clash + PIP_CACHE_DIR) using $Image ..."
    try {
        $args = @(
            "run", "--rm", "--platform", "linux/amd64",
            "--add-host=host.docker.internal:host-gateway",
            "-e", "PIP_CACHE_DIR=/root/.cache/pip",
            "-e", "HTTP_PROXY=$ClashContainer",
            "-e", "HTTPS_PROXY=$ClashContainer",
            "-e", "http_proxy=$ClashContainer",
            "-e", "https_proxy=$ClashContainer",
            "-e", "NO_PROXY=localhost,127.0.0.1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "PIP_PROGRESS_BAR=on",
            "-e", "PIP_DEFAULT_TIMEOUT=300",
            "-v", "${PipCacheDir}:/root/.cache/pip",
            "-v", "${AfDir}:/agentflow-src:ro",
            "-v", "${sh}:/tb2-prewarm.sh:ro",
            $Image,
            "bash", "/tb2-prewarm.sh"
        )
        & docker @args
        if ($LASTEXITCODE -ne 0) {
            throw "docker pip prewarm failed on $Image (exit $LASTEXITCODE)"
        }
    }
    finally {
        Remove-Item -LiteralPath $sh -Force -ErrorAction SilentlyContinue
    }
}

# Keep SHARED_PKGS in sync with env_install_common.sh.
$pipCmd = @'
set -e
export PIP_CACHE_DIR=/root/.cache/pip
export PIP_DEFAULT_TIMEOUT=300
export PYTHONUNBUFFERED=1
export PIP_PROGRESS_BAR=on
python -u -m pip install -U pip
python -m venv /tmp/tb2-prewarm
. /tmp/tb2-prewarm/bin/activate
python -u -m pip install -U pip setuptools wheel
echo PREWARM mini-swe-agent autogen-agentchat Magentic-One hatchling
python -u -m pip install --prefer-binary --retries 10 --progress-bar on \
    setuptools wheel hatchling mini-swe-agent \
    "autogen-agentchat==0.7.5" "autogen-ext[magentic-one,openai]==0.7.5"
echo PREWARM common-env shared pip libraries
SHARED_PKGS="openai numpy pandas aiohttp tiktoken pydantic PyYAML GitPython typing_extensions tenacity rich typer loguru python-docx openpyxl beautifulsoup4 fire aiofiles tqdm wrapt typing-inspect libcst websocket-client socksio gitignore-parser websockets networkx anytree Pillow nbclient nbformat ipython ipykernel scikit-learn imap-tools chardet playwright google-generativeai anthropic zhipuai qianfan dashscope jieba rank-bm25"
# shellcheck disable=SC2086
python -u -m pip install --prefer-binary --retries 10 --progress-bar on $SHARED_PKGS
echo PREWARM AgentFlow requirements minus vllm easyocr transformers
grep -E -v '^(vllm|easyocr|transformers)(==|[[:space:]]|$)' \
    /agentflow-src/agentflow/requirements.txt | grep -v '^#' > /tmp/af-req.txt || true
echo PREWARM af-req:
cat /tmp/af-req.txt
set +e
python -u -m pip install --prefer-binary --retries 10 --progress-bar on -r /tmp/af-req.txt
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    echo PREWARM_WARN AgentFlow remaining requirements failed; continuing
fi
echo PREWARM pip cache file count
find "$PIP_CACHE_DIR" -type f | wc -l
'@

foreach ($tag in $PythonTags) {
    Invoke-PipPrewarm -Image "python:$tag" -InnerCmd $pipCmd
}

@"
agentflow=$AfDir
pip-cache=$PipCacheDir
"@ | Set-Content -Path $OkMarker -Encoding utf8

Write-Host ""
Write-Host "Done."
Write-Host "  agentflow source: $AfDir"
Write-Host "  pip cache:        $PipCacheDir (mount /root/.cache/pip)"
Write-Host "Re-run: .\scripts\cache_agent_packages.ps1"
