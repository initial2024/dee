$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$control = Join-Path $root 'scripts\xiaoyu-router-control.ps1'
$shortcutScript = Join-Path $root 'scripts\install-router-desktop-shortcut.ps1'
$output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -NoShow 2>&1
$text = $output -join "`n"
Assert-True ($text -match 'CODEX_STATUS_VISIBLE=YES') 'desktop_control_codex_status_visible'
Assert-True ($text -match 'PROVIDER_LIST_VISIBLE=YES') 'desktop_control_provider_list_visible'
Assert-True ($text -match 'USAGE_GUARD_VISIBLE=YES') 'desktop_control_usage_guard_visible'
Assert-True ($text -match 'SECRET_VALUES_VISIBLE=NO') 'desktop_control_hides_secret_values'
  $ui = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -SelfTest 2>&1
  Assert-True (($ui -join "`n") -match 'CONTROL_UI_INITIALIZATION=PASS') 'desktop_control_initializes_without_home_variable_error'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'Official remaining quota: open the official Usage panel') 'official_quota_not_faked'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'Router Direct Smoke') 'safe_router_smoke_button_present'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'OpenAI \+ Handoff') 'desktop_control_exposes_handoff_openai_profile'

$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-desktop-shortcut-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $shortcutOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shortcutScript -DesktopPath $temp 2>&1
  $shortcut = Get-ChildItem -LiteralPath $temp -Filter '*.lnk' | Select-Object -First 1
  Assert-True (($shortcutOutput -join "`n") -match 'NO_ADMIN_REQUIRED=YES') 'shortcut_requires_no_admin'
  Assert-True ($null -ne $shortcut) 'shortcut_created'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output 'POWERSHELL_TEST_TOTAL=10'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
