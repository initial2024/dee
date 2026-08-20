[CmdletBinding()]
param(
  [switch]$ToXiaoyu,
  [switch]$ToOpenAI,
  [ValidateSet('light','smart','strong')]
  [string]$ModelProfile = 'light',
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$Task = 'Continue the current Codex task',
  [string]$HandoffPath = ''
)

$ErrorActionPreference = 'Stop'
if ($ToXiaoyu -eq $ToOpenAI) { throw 'Choose exactly one of -ToXiaoyu or -ToOpenAI.' }

$handoffArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $PSScriptRoot 'make-codex-handoff.ps1'),'-ProjectRoot',$ProjectRoot,'-Task',$Task)
if (-not [string]::IsNullOrWhiteSpace($HandoffPath)) { $handoffArgs += @('-OutputPath',$HandoffPath) }
& powershell.exe @handoffArgs
if ($LASTEXITCODE -ne 0) { throw 'Handoff generation failed; configuration was not switched.' }

if ($ToXiaoyu) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-xiaoyu-codex.ps1') -ConfigPath $ConfigPath
  if ($LASTEXITCODE -ne 0) { throw 'Switch to XiaoyuRouter failed after handoff generation.' }
  Write-Output 'SWITCH_TARGET=XIAOYU_ROUTER'
  Write-Output 'CODEX_OUTER_MODEL_STABLE=YES'
  exit 0
}

$profile = @{
  light = @{ model = 'gpt-5.6-luna'; reasoning = 'low' }
  smart = @{ model = 'gpt-5.6-terra'; reasoning = 'medium' }
  strong = @{ model = 'gpt-5.6-sol'; reasoning = 'high' }
}[$ModelProfile]
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-openai-codex.ps1') -ConfigPath $ConfigPath -TestModel $profile.model -TestReasoning $profile.reasoning
if ($LASTEXITCODE -ne 0) { throw 'Switch to OpenAI failed after handoff generation.' }
Write-Output 'SWITCH_TARGET=OPENAI'
Write-Output ('MODEL_PROFILE=' + $ModelProfile)
