$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$control = Get-Content -LiteralPath (Join-Path $root 'scripts\xiaoyu-router-control.ps1') -Raw -Encoding UTF8
$selector = Get-Content -LiteralPath (Join-Path $root 'src\codex_ai_router\providers\local_model_selector.py') -Raw -Encoding UTF8
$cli = Get-Content -LiteralPath (Join-Path $root 'src\codex_ai_router\cli.py') -Raw -Encoding UTF8
$delegate = Get-Content -LiteralPath (Join-Path $root 'scripts\xiaoyu-delegate.ps1') -Raw -Encoding UTF8

Assert-True ($selector -match 'vision_projector' -and $selector -match 'parameter_guess' -and $selector -match 'manual_only') 'local_profiles_include_required_fields'
Assert-True ($selector -match 'MMPROJ_NOT_TEXT_MODEL') 'mmproj_is_not_text_model'
Assert-True ($selector -match 'BF16_AUTO_DISABLED' -and $selector -match 'HEAVY_MODEL_REQUIRES_HIGH_THRESHOLD') 'heavy_models_are_guarded'
Assert-True ($selector -match 'LOCAL_HIGH_RISK_SAFE_STOP' -and $selector -match 'LOCAL_NO_ELIGIBLE_MODEL') 'local_safe_stop_codes_present'
Assert-True ($selector -match 'manual_disabled_models' -and $selector -match 'manual_only_model' -and $selector -match 'manual_preferred_model') 'manual_policy_priority_fields_present'
Assert-True ($cli -match 'local_profiles' -and $cli -match 'local_explain' -and $cli -match 'local_auto' -and $cli -match 'local_policy') 'local_cli_commands_present'
Assert-True ($delegate -match 'local auto-smoke --task') 'delegate_uses_auto_selector'
Assert-True ($control -match '\u67e5\u770b\u6a21\u578b\u753b\u50cf' -and $control -match '\u89e3\u91ca\u9009\u62e9' -and $control -match '\u81ea\u52a8\u9009\u62e9\u6a21\u578b') 'local_management_buttons_are_chinese'
Assert-True ($control -match '\u5141\u8bb8\u6162\u6a21\u578b' -and $control -match '\u7981\u6b62 BF16 \u81ea\u52a8\u9009\u62e9' -and $control -match '\u6253\u5f00\u914d\u7f6e\u6587\u4ef6' -and $control -match '\u6253\u5f00\u65e5\u5fd7\u76ee\u5f55') 'local_policy_buttons_are_chinese'
Assert-True ($control -match '\u7aef\u53e3\u88ab\u5360\u7528' -and $control -match '\u6a21\u578b\u52a0\u8f7d\u8d85\u65f6' -and $control -match '\u9ad8\u98ce\u9669\u4efb\u52a1\u5df2\u505c\u6b62') 'error_codes_have_chinese_explanations'
Assert-True ($control -match '\u9ad8\u7ea7\u8c03\u8bd5\u5b57\u6bb5' -and $control -match '\u63a7\u5236\u53f0\u4ec5\u8d1f\u8d23\u7ba1\u7406') 'technical_fields_are_advanced_and_management_only'

Write-Output "POWERSHELL_TEST_TOTAL=11"
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
