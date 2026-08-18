param([switch]$UserInstall)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { & $python.Source -m pip install --user $root } else { py -3 -m pip install --user $root }
$scripts = (& py -3 -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))").Trim()
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $scripts) {
  [Environment]::SetEnvironmentVariable('Path', (($userPath.TrimEnd(';') + ';' + $scripts).TrimStart(';')), 'User')
}
if (($env:Path -split ';') -notcontains $scripts) { $env:Path += ';' + $scripts }
Write-Host 'Installed xiaoyu-router for the current user. It is available now and in new PowerShell sessions.'
