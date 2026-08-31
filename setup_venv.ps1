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
Write-Host "Run:"
Write-Host "  python run_bench.py               # 默认 seed=42 抽 30 题，四变体全跑"
Write-Host "  python run_bench.py 1 --end 8     # 只跑抽样前 8 题"
Write-Host "  python run_bench.py --sy1         # 只跑 logorythia1"
Write-Host "Acc denominator = SAMPLE_SIZE (30); corpus = ./tasks (TB 2.1: 89)."
