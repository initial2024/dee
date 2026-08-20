$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $root 'scripts\codex-usage.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-codex-usage-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $config = Join-Path $temp 'config.toml'
  $ledger = Join-Path $temp 'usage-ledger.jsonl'
  [IO.File]::WriteAllText($config, "model = `"gpt-5.6-luna`"`nmodel_reasoning_effort = `"low`"`n[model_providers.XiaoyuRouter]`nbase_url = `"http://127.0.0.1:18789/v1`"`nwire_api = `"responses`"`n", (New-Object Text.UTF8Encoding($false)))
  [IO.File]::WriteAllText($ledger, '{"timestamp":"2026-08-20T00:00:00+00:00","active_provider":"OpenAI","router_virtual_model":"xiaoyu-lightboat","local_provider_used":"YES","success":false,"error_code":"TIMEOUT","prompt":"never-display"}' + "`n", (New-Object Text.UTF8Encoding($false)))
  $openai = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -ConfigPath $config -UsageLedgerPath $ledger 2>&1
  $openaiText = $openai -join "`n"
  Assert-True ($openaiText -match 'LIKELY_CONSUMING_OPENAI_CODEX_QUOTA=YES') 'openai_warns_about_official_quota'
  Assert-True ($openaiText -match 'NO_FAKE_CODEX_QUOTA=YES') 'official_quota_not_faked'
  Assert-True ($openaiText -match 'NO_COOKIE_OR_TOKEN_READING=YES') 'no_cookie_or_token_reading'
  Assert-True ($openaiText -notmatch 'never-display') 'ledger_prompt_not_displayed'
  $save = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -ConfigPath $config -UsageLedgerPath $ledger -SaveMode 2>&1
  $saveText = $save -join "`n"
  Assert-True ($saveText -match 'ACTIVE_PROVIDER=XiaoyuRouter') 'save_mode_calls_xiaoyu_switcher'
  Assert-True ($saveText -match 'LIKELY_CONSUMING_OPENAI_CODEX_QUOTA=NO_FOR_MODEL_INFERENCE') 'xiaoyu_mode_shows_savings_notice'
  $restore = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -ConfigPath $config -UsageLedgerPath $ledger -OpenAIMode 2>&1
  $restoreText = $restore -join "`n"
  $restored = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True ($restoreText -match 'ACTIVE_MODEL=gpt-5.6-luna') 'openai_mode_calls_light_profile_switcher'
  Assert-True ($restored -match '(?m)^model_reasoning_effort = "low"') 'openai_mode_keeps_low_reasoning'
  Assert-True ((Get-Content -LiteralPath $script -Raw -Encoding UTF8) -match 'chatgpt.com/#settings') 'official_usage_link_is_available'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output 'POWERSHELL_TEST_TOTAL=9'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
