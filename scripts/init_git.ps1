$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path .git)) { git init }
git add .
Write-Output "Git repository initialized and files staged. Review with: git status"
