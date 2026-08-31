# Create a Windows-native venv and install harness dependencies.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (Test-Path ".venv") {
    Write-Host "Removing existing .venv ..."
    Remove-Item -Recurse -Force ".venv"
}

Write-Host "Creating .venv ..."
py -3 -m venv .venv
if ($LASTEXITCODE -ne 0) {
    python -m venv .venv
}

$Pip = Join-Path $Root ".venv\Scripts\pip.exe"

& $Pip install --upgrade pip
& $Pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Activate with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "Then (optional caches):"
Write-Host "  .\.cache\start_dnf_cacher.ps1   # Squid :3144 (apt/dnf; upstream Clash)"
Write-Host "  .\.cache\cache_claude.ps1"
Write-Host "  .\.cache\cache_agent_packages.ps1  # AgentFlow src + .cache/pip"
Write-Host "Run:"
Write-Host "  python run_bench.py 1 --logorythia --claude"
Write-Host "Acc denominator = number of tasks in ./tasks (TB 2.1: 89)."
