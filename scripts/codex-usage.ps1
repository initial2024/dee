[CmdletBinding()]
param(
  [switch]$OpenUsage,
  [switch]$SaveMode,
  [switch]$OpenAIMode,
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$UsageLedgerPath = (Join-Path $env:USERPROFILE '.codex-ai-router\usage-ledger.jsonl')
)

$ErrorActionPreference = 'Stop'

function Get-TopLevelValue([string[]]$Lines, [string]$Name) {
  foreach ($line in $Lines) {
    if ($line -match '^\s*\[') { break }
    if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*"([^"]*)"')) { return $Matches[1] }
  }
  return $null
}

function Get-UsageSummary([string]$Path) {
  $summary = @{ today = 0; week = 0; openai = 0; xiaoyu = 0; lightboat = 0; local = 0; failed = 0 }
  if (-not (Test-Path -LiteralPath $Path)) { return $summary }
  $today = (Get-Date).ToUniversalTime().Date
  $weekStart = (Get-Date).ToUniversalTime().AddDays(-7)
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    try {
      $entry = $line | ConvertFrom-Json
      $at = ([datetime]$entry.timestamp).ToUniversalTime()
      if ($at.Date -eq $today) { $summary.today++ }
      if ($at -lt $weekStart) { continue }
      $summary.week++
      if ($entry.active_provider -in @('OpenAI','DEFAULT')) { $summary.openai++ }
      if ($entry.active_provider -eq 'XiaoyuRouter') { $summary.xiaoyu++ }
      if (("$($entry.router_virtual_model)$($entry.estimated_route)") -match '(?i)lightboat') { $summary.lightboat++ }
      if ($entry.local_provider_used -eq 'YES') { $summary.local++ }
      if (-not $entry.success -or "$($entry.error_code)" -match 'TIMEOUT') { $summary.failed++ }
    } catch {}
  }
  return $summary
}

if ($SaveMode -and $OpenAIMode) { throw 'Choose either -SaveMode or -OpenAIMode, not both.' }
if ($SaveMode) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-xiaoyu-codex.ps1') -ConfigPath $ConfigPath }
if ($OpenAIMode) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-openai-codex.ps1') -ConfigPath $ConfigPath }
if ($OpenUsage) { Start-Process 'https://chatgpt.com/#settings' }
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Codex config.toml was not found.' }

$lines = Get-Content -LiteralPath $ConfigPath -Encoding UTF8
$provider = Get-TopLevelValue $lines 'model_provider'
$model = Get-TopLevelValue $lines 'model'
if ([string]::IsNullOrWhiteSpace($provider)) { $provider = 'DEFAULT' }
if ([string]::IsNullOrWhiteSpace($model)) { $model = 'DEFAULT' }
$summary = Get-UsageSummary $UsageLedgerPath

if ($provider -eq 'XiaoyuRouter') {
  $quota = 'NO_FOR_MODEL_INFERENCE'
  $recommended = 'XIAOYU_ROUTER_SAVE_MODE'
  $notice = 'Codex App or Agent shell service usage is not proven zero and requires separate measurement.'
} else {
  $quota = 'YES'
  $recommended = 'OPENAI_LIGHT_TEST_PROFILE_OR_XIAOYU_ROUTER_SAVE_MODE'
  $notice = 'Use the official Usage panel for account quota; local counts are not official quota.'
}

Write-Output ('ACTIVE_PROVIDER=' + $provider)
Write-Output ('ACTIVE_MODEL=' + $model)
Write-Output ('LIKELY_CONSUMING_OPENAI_CODEX_QUOTA=' + $quota)
Write-Output 'OFFICIAL_USAGE_VIEW=OPEN_IN_USAGE_PANEL'
Write-Output ('LOCAL_USAGE_LEDGER_SUMMARY=TODAY=' + $summary.today + ';WEEK=' + $summary.week + ';OPENAI=' + $summary.openai + ';XIAOYU_ROUTER=' + $summary.xiaoyu + ';LIGHTBOAT=' + $summary.lightboat + ';LOCAL=' + $summary.local + ';FAILED_OR_TIMEOUT=' + $summary.failed)
Write-Output ('RECOMMENDED_MODE=' + $recommended)
Write-Output ('NOTICE=' + $notice)
Write-Output 'NO_FAKE_CODEX_QUOTA=YES'
Write-Output 'NO_COOKIE_OR_TOKEN_READING=YES'
Write-Output 'DEFAULT_OPENAI_TEST_PROFILE=YES'
Write-Output 'DEFAULT_OPENAI_TEST_MODEL=5.6_LUNA'
Write-Output 'DEFAULT_OPENAI_TEST_REASONING=LIGHT_OR_LOW'
