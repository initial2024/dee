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
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match '本控制台不会伪造额度') 'official_quota_not_faked'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'Router 直连测试') 'safe_router_smoke_button_present'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match '使用 OpenAI Luna（低）') 'desktop_control_exposes_handoff_openai_profile'
$source = Get-Content -LiteralPath $control -Raw -Encoding UTF8
Assert-True ($source -match '小羽 Router 控制台' -and $source -match '使用小羽 Router' -and $source -match '使用 OpenAI Luna（低）') 'chinese_title_and_buttons_present'
Assert-True ($source -match "Microsoft YaHei UI" -and $source -match '\[double\]\$FontScale = 1\.15') 'large_font_and_scale_parameter_present'
Assert-True ($source -match 'AutoScaleMode.*Dpi') 'dpi_scaling_is_enabled'
Assert-True ($source -match 'Router：\{0\}`r`n监听地址：\{1\}') 'status_fields_are_line_separated'
Assert-True ($source -notmatch '(?i)api[_ -]?key\s*=' -and $source -notmatch '(?i)authorization\s*=') 'ui_does_not_embed_secret_values'

$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-desktop-shortcut-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $shortcutOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shortcutScript -DesktopPath $temp 2>&1
  $shortcut = Get-ChildItem -LiteralPath $temp -Filter '*.lnk' | Select-Object -First 1
  Assert-True (($shortcutOutput -join "`n") -match 'NO_ADMIN_REQUIRED=YES') 'shortcut_requires_no_admin'
  Assert-True ($null -ne $shortcut) 'shortcut_created'
  Assert-True ((Get-Content -LiteralPath $shortcutScript -Raw -Encoding UTF8) -match 'FontScale 1\.2') 'shortcut_uses_default_font_scale'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output 'POWERSHELL_TEST_TOTAL=16'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
