[CmdletBinding()]
param(
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$TestModel = 'gpt-5.6-luna',
  [ValidateSet('low','medium','high','light')]
  [string]$TestReasoning = 'low'
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
function Get-ReasoningKey([string[]]$Lines) {
  foreach ($line in $Lines) {
    if ($line -match '^\s*\[') { break }
    if ($line -match '^\s*model_reasoning_effort\s*=') { return 'model_reasoning_effort' }
    if ($line -match '^\s*reasoning_effort\s*=') { return 'reasoning_effort' }
  }
  return 'model_reasoning_effort'
}
function Save-Utf8Atomic([string]$Path, [string[]]$Lines) {
  $target = [IO.Path]::GetFullPath($Path); $temp = $target + '.xiaoyu-router.tmp'
  [IO.File]::WriteAllText($temp, (($Lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
  Move-Item -LiteralPath $temp -Destination $target -Force
}

$statePath = Join-Path (Split-Path -Parent $ConfigPath) 'xiaoyu-router-switch-state.json'
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Codex config.toml was not found.' }
$repairStateRoot = Join-Path (Split-Path -Parent $ConfigPath) '.xiaoyu-wire-api-repair'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'codex-mode-manager.ps1') -Action repair-wire-api -ConfigPath $ConfigPath -StateRoot $repairStateRoot | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Codex wire_api repair failed before official-mode switch.' }
$state = if (Test-Path -LiteralPath $statePath) {
  Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
  [pscustomobject]@{ provider_id = $null }
}
$lines = [System.Collections.Generic.List[string]]::new([string[]](Get-Content -LiteralPath $ConfigPath -Encoding UTF8))
foreach ($name in 'model_provider') {
  $value = $state.$name
  if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) { Remove-TopLevelKey $lines $name } else { Set-TopLevelString $lines $name ([string]$value) }
}
$reasoningKey = Get-ReasoningKey $lines
Set-TopLevelString $lines 'model' $TestModel
Set-TopLevelString $lines $reasoningKey $TestReasoning
Save-Utf8Atomic $ConfigPath $lines
Write-Output ('ACTIVE_PROVIDER=' + $(if ($state.provider_id) { $state.provider_id } else { 'DEFAULT' }))
Write-Output ('ACTIVE_MODEL=' + $TestModel)
Write-Output ('OPENAI_LIGHT_TEST_MODEL_ID=' + $TestModel)
Write-Output ('OPENAI_LIGHT_TEST_REASONING_VALUE=' + $TestReasoning)
Write-Output 'SCHEMA_ALLOWED_VALUE_CHECK=YES'
Write-Output 'CONFIG_WRITE_ATOMIC=YES'
