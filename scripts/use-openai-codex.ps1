[CmdletBinding()]
param(
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml')
)

$ErrorActionPreference = 'Stop'
function Set-TopLevelString([System.Collections.Generic.List[string]]$Lines, [string]$Name, [string]$Value) {
  $sectionAt = $Lines.Count
  for ($i = 0; $i -lt $Lines.Count; $i++) { if ($Lines[$i] -match '^\s*\[') { $sectionAt = $i; break } }
  for ($i = 0; $i -lt $sectionAt; $i++) { if ($Lines[$i] -match ('^\s*' + [regex]::Escape($Name) + '\s*=')) { $Lines[$i] = $Name + ' = "' + $Value + '"'; return } }
  $Lines.Insert($sectionAt, $Name + ' = "' + $Value + '"')
}
function Remove-TopLevelKey([System.Collections.Generic.List[string]]$Lines, [string]$Name) {
  for ($i = 0; $i -lt $Lines.Count; $i++) { if ($Lines[$i] -match '^\s*\[') { break }; if ($Lines[$i] -match ('^\s*' + [regex]::Escape($Name) + '\s*=')) { $Lines.RemoveAt($i); return } }
}
function Save-Utf8Atomic([string]$Path, [string[]]$Lines) {
  $target = [IO.Path]::GetFullPath($Path); $temp = $target + '.xiaoyu-router.tmp'
  [IO.File]::WriteAllText($temp, (($Lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
  Move-Item -LiteralPath $temp -Destination $target -Force
}

$statePath = Join-Path (Split-Path -Parent $ConfigPath) 'xiaoyu-router-switch-state.json'
if (-not (Test-Path -LiteralPath $ConfigPath) -or -not (Test-Path -LiteralPath $statePath)) { throw 'Saved pre-Xiaoyu Codex state was not found.' }
$state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
$lines = [System.Collections.Generic.List[string]]::new([string[]](Get-Content -LiteralPath $ConfigPath -Encoding UTF8))
foreach ($name in 'model_provider','model','reasoning_effort') {
  $value = $state.$name
  if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) { Remove-TopLevelKey $lines $name } else { Set-TopLevelString $lines $name ([string]$value) }
}
Save-Utf8Atomic $ConfigPath $lines
Write-Output ('ACTIVE_PROVIDER=' + $(if ($state.provider_id) { $state.provider_id } else { 'DEFAULT' }))
Write-Output ('ACTIVE_MODEL=' + $(if ($state.model) { $state.model } else { 'DEFAULT' }))
Write-Output 'CONFIG_WRITE_ATOMIC=YES'
