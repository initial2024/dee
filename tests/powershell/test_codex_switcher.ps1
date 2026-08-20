$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-switcher-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $config = Join-Path $temp 'config.toml'
  [IO.File]::WriteAllText($config, "model = `"openai-existing`"`n[model_providers.XiaoyuRouter]`nbase_url = `"http://127.0.0.1:18789/v1`"`nwire_api = `"responses`"`n", (New-Object Text.UTF8Encoding($false)))
  $activate = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\use-xiaoyu-codex.ps1') -ConfigPath $config 2>&1
  $active = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True (($activate -join "`n") -match 'ACTIVE_PROVIDER=XiaoyuRouter') 'switch_detects_real_provider_id'
  Assert-True ($active -match '(?m)^model_provider = "XiaoyuRouter"') 'switch_sets_provider'
  Assert-True ($active -match '(?m)^model = "xiaoyu-lightboat"') 'switch_sets_virtual_model'
  Assert-True ($active -match '\[model_providers\.XiaoyuRouter\]') 'switch_preserves_provider_sections'
  Assert-True (Test-Path -LiteralPath ($config + '.xiaoyu-router.bak')) 'backup_created'
  $restore = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\use-openai-codex.ps1') -ConfigPath $config 2>&1
  $restored = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True (($restore -join "`n") -match 'ACTIVE_MODEL=openai-existing') 'restore_recovers_model'
  Assert-True ($restored -match '(?m)^model = "openai-existing"') 'restore_sets_original_model'
  Assert-True ($restored -notmatch '(?m)^model_provider =') 'restore_removes_absent_original_provider'
  Assert-True ($restored -match '\[model_providers\.XiaoyuRouter\]') 'restore_keeps_xiaoyu_provider'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }
Write-Output 'POWERSHELL_TEST_TOTAL=9'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
