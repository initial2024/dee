param([switch]$UserInstall)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { & $python.Source -m pip install --user $root } else { py -3 -m pip install --user $root }
Write-Host 'Installed xiaoyu-router for the current user. Open a new PowerShell if PATH was refreshed.'
