$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$delegate = Join-Path $root 'scripts\xiaoyu-delegate.ps1'
$officialMode = Join-Path $root 'scripts\use-official-delegation-codex.ps1'
$instruction = Join-Path $root '.codex-ai-router\official-delegation-instructions.md'
$usage = Join-Path $root 'scripts\codex-usage.ps1'
$delegateText = Get-Content -LiteralPath $delegate -Raw -Encoding UTF8
$officialModeText = Get-Content -LiteralPath $officialMode -Raw -Encoding UTF8
$usageText = Get-Content -LiteralPath $usage -Raw -Encoding UTF8

Assert-True ($delegateText -match '\[switch\]\$AllowWrite') 'delegate_accepts_explicit_write_flag'
Assert-True ($delegateText -match 'Read-only advisory task') 'delegate_defaults_readonly'
Assert-True ($delegateText -match 'delegation-ledger.jsonl') 'delegation_ledger_present'
Assert-True ($delegateText -match 'delegate-fast --max-seconds \$budget') 'delegate_passes_user_timeout_to_fast_route'
Assert-True ($delegateText -match 'provider=\$payload.provider;model=\$payload.model') 'ledger_records_selected_provider_and_model'
Assert-True ($delegateText -notmatch '(?i)api[_ -]?key\s*=\s*["''][^"'']+') 'delegate_has_no_embedded_key'
Assert-True ($officialModeText -match 'high-risk.*official Codex control') 'instructions_keep_high_risk_official'
Assert-True ($officialModeText -match '(?i)do not copy official internal cache') 'instructions_make_no_cache_copy_claim'
Assert-True ($usageText -match 'ACTIVE_REASONING=') 'usage_shows_active_reasoning'
Assert-True ($usageText -match 'DELEGATION_DOES_NOT_AUTO_CHANGE_MODEL_NOTICE') 'usage_warns_about_no_hot_switch'
Assert-True ($usageText -match 'HIGH_RISK_STRONG_MODEL_RECOMMENDATION=YES') 'usage_shows_strong_model_guidance'
Assert-True ($usageText -notmatch '(?i)Get-Content[^\r\n]*(cookie|token)|\.sqlite|Cookies\\') 'usage_does_not_read_cookie_or_token'

Write-Output 'POWERSHELL_TEST_TOTAL=12'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
