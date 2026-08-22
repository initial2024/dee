[CmdletBinding()]
param(
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$RouterBaseUrl = 'http://127.0.0.1:18789/v1'
)

$ErrorActionPreference = 'Stop'

function Get-TopLevelValue([string[]]$Lines, [string]$Name) {
  foreach ($line in $Lines) {
    if ($line -match '^\s*\[') { break }
    if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*"([^"]*)"\s*(?:#.*)?$')) { return $Matches[1] }
  }
  return $null
}

function Set-TopLevelString([System.Collections.Generic.List[string]]$Lines, [string]$Name, [string]$Value) {
  $sectionAt = $Lines.Count
  for ($i = 0; $i -lt $Lines.Count; $i++) { if ($Lines[$i] -match '^\s*\[') { $sectionAt = $i; break } }
  for ($i = 0; $i -lt $sectionAt; $i++) {
    if ($Lines[$i] -match ('^\s*' + [regex]::Escape($Name) + '\s*=')) { $Lines[$i] = $Name + ' = "' + $Value + '"'; return }
  }
  $Lines.Insert($sectionAt, $Name + ' = "' + $Value + '"')
}

function Save-Utf8Atomic([string]$Path, [string[]]$Lines) {
  $target = [IO.Path]::GetFullPath($Path); $temp = $target + '.xiaoyu-router.tmp'
  [IO.File]::WriteAllText($temp, (($Lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
  Move-Item -LiteralPath $temp -Destination $target -Force
}

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Codex config.toml was not found.' }
$repairStateRoot = Join-Path (Split-Path -Parent $ConfigPath) '.xiaoyu-wire-api-repair'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'codex-mode-manager.ps1') -Action repair-wire-api -ConfigPath $ConfigPath -StateRoot $repairStateRoot | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Codex wire_api repair failed before Xiaoyu Router switch.' }
$source = Get-Content -LiteralPath $ConfigPath -Encoding UTF8
$providerId = $null; $section = $null
foreach ($line in $source) {
  if ($line -match '^\s*\[model_providers\.([^\]]+)\]\s*$') { $section = $Matches[1]; continue }
  if ($line -match '^\s*\[') { $section = $null; continue }
  if ($section -and $line -match '^\s*base_url\s*=\s*"([^"]*)"') {
    if ($Matches[1].TrimEnd('/') -eq $RouterBaseUrl.TrimEnd('/')) { $providerId = $section; break }
  }
}
if (-not $providerId) { throw 'No Xiaoyu Router provider pointing at the configured localhost URL was found.' }

$statePath = Join-Path (Split-Path -Parent $ConfigPath) 'xiaoyu-router-switch-state.json'
$state = @{ provider_id = (Get-TopLevelValue $source 'model_provider'); model = (Get-TopLevelValue $source 'model'); model_reasoning_effort = (Get-TopLevelValue $source 'model_reasoning_effort'); reasoning_effort = (Get-TopLevelValue $source 'reasoning_effort') }
[IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
Copy-Item -LiteralPath $ConfigPath -Destination ($ConfigPath + '.xiaoyu-router.bak') -Force
$lines = [System.Collections.Generic.List[string]]::new([string[]]$source)
Set-TopLevelString $lines 'model_provider' $providerId
Set-TopLevelString $lines 'model' 'xiaoyu-auto'
Save-Utf8Atomic $ConfigPath $lines
Write-Output ('ACTIVE_PROVIDER=' + $providerId)
Write-Output 'ACTIVE_MODEL=xiaoyu-auto'
Write-Output 'CODEX_OUTER_MODEL_STABLE=YES'
Write-Output 'CONFIG_WRITE_ATOMIC=YES'
