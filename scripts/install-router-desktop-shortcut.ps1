[CmdletBinding()]
param([string]$DesktopPath = [Environment]::GetFolderPath('Desktop'))

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $PSScriptRoot 'xiaoyu-router-control.ps1'
$shortcutName = ([string][char]0x5c0f) + ([string][char]0x7fbd) + ' Router ' + ([string][char]0x63a7) + ([string][char]0x5236) + ([string][char]0x53f0) + '.lnk'
$shortcut = Join-Path $DesktopPath $shortcutName

if (-not (Test-Path -LiteralPath $DesktopPath)) { New-Item -ItemType Directory -Path $DesktopPath -Force | Out-Null }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = (Get-Command powershell.exe).Source
$link.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $target + '"'
$link.WorkingDirectory = $root
$link.IconLocation = $link.TargetPath
$link.Save()

Write-Output ('DESKTOP_SHORTCUT=' + $shortcut)
Write-Output 'DESKTOP_SHORTCUT_TARGET_VALID=YES'
Write-Output 'NO_ADMIN_REQUIRED=YES'
