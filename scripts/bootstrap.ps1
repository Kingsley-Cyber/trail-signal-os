$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
niche-research validate
python -m unittest discover -s tests -v
Write-Output "BOOTSTRAP PASS"
