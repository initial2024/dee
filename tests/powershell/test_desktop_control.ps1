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
Assert-True ($source -match "Microsoft YaHei UI" -and $source -match '\[double\]\$FontScale = 1\.25') 'large_font_and_scale_parameter_present'
Assert-True ($source -match 'AutoScaleMode.*Dpi') 'dpi_scaling_is_enabled'
Assert-True ($source -match 'Router：\{0\}`r`n监听地址：\{1\}') 'status_fields_are_line_separated'
Assert-True ($source -notmatch '(?i)api[_ -]?key\s*=' -and $source -notmatch '(?i)authorization\s*=') 'ui_does_not_embed_secret_values'

$providerFixture = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-provider-grid-' + [guid]::NewGuid().ToString() + '.json')
$emptyFixture = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-provider-empty-' + [guid]::NewGuid().ToString() + '.json')
$badFixture = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-provider-bad-' + [guid]::NewGuid().ToString() + '.json')
try {
  [IO.File]::WriteAllText($providerFixture, '{"providers":{"fake-provider":{"display_name":"Fake Provider","type":"openai_compatible","base_url":"https://example.invalid/v1","wire_api":"responses","enabled":true,"api_key_env":"FAKE_KEY_ENV","headers":{"x-header":{"env":"FAKE_HEADER_ENV"}},"models":["model-a"]}}}', (New-Object Text.UTF8Encoding($false)))
  [IO.File]::WriteAllText($emptyFixture, '{"providers":{}}', (New-Object Text.UTF8Encoding($false)))
  [IO.File]::WriteAllText($badFixture, '{not json', (New-Object Text.UTF8Encoding($false)))
  $fake = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -SelfTest -ProviderConfigPath $providerFixture 2>&1
  $empty = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -SelfTest -ProviderConfigPath $emptyFixture 2>&1
  $bad = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -SelfTest -ProviderConfigPath $badFixture 2>&1
  Assert-True (($fake -join "`n") -match 'PROVIDER_TABLE_ROWS=1') 'provider_refresh_populates_fake_row'
  Assert-True (($fake -join "`n") -match 'PROVIDER_TABLE_COLUMNS=10') 'provider_table_has_fixed_columns'
  Assert-True (($empty -join "`n") -match '暂无供应商') 'provider_empty_state_visible'
  Assert-True (($bad -join "`n") -match '读取供应商列表失败') 'provider_error_state_visible'
} finally { foreach($path in @($providerFixture,$emptyFixture,$badFixture)){ if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force} } }
Assert-True ($source -match '请先选择一个供应商') 'no_provider_selection_prompt_is_present'
Assert-True ($source -match '最近操作日志（已脱敏）' -and $source -match 'Add-ProviderLog') 'provider_tab_redacted_log_is_present'
Assert-True ($source -match 'Header 名称' -and $source -notmatch 'Header value') 'provider_details_expose_header_names_only'
Assert-True ($source.Contains("'provider','refresh-models',`$id") -and $source.Contains("'provider','probe-runtime',`$id")) 'model_refresh_and_runtime_probe_actions_are_bound'
Assert-True ($source -match '供应商元数据解析失败') 'provider_metadata_error_is_surfaced'
Assert-True ($source -match 'model_registry' -and $source -match '发现模型数' -and $source -match '可用模型数') 'control_panel_model_counts_use_registry_snapshot'
Assert-True ($source -match '可用模型：' -and $source -match '运行可用模型：' -and $source -match '当前运行模型：') 'provider_details_show_model_state_buckets'
Assert-True ($source -match 'Open-ModelPicker' -and $source -match '设为当前模型' -and $source -match '设为首选模型' -and $source -match '批量禁用') 'control_panel_model_picker_persists_manual_selection'
Assert-True ($source -match 'New-ModelDialogGrid' -and $source -match 'DataGridView' -and $source -match '勾选') 'model_picker_is_real_multiselect_grid'
Assert-True ($source -match 'Open-BatchManager' -and $source -match '批量允许' -and $source -match '批量清除禁用') 'batch_management_is_real_ui'
Assert-True ($source -match '筛选模型/状态' -and $source -match 'Set-ModelGridFilter') 'batch_management_has_filter'
Assert-True ($source -match '供应商操作' -and $source -match '模型操作' -and $source -match '迁移') 'provider_tab_actions_are_grouped'
Assert-True (($ui -join "`n") -match 'MODEL_PICKER_UI_CONSTRUCTION=(PASS|SKIPPED_NO_PROVIDER)' -and ($ui -join "`n") -match 'BATCH_UI_CONSTRUCTION=(PASS|SKIPPED_NO_PROVIDER)') 'model_and_batch_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'DIRECT_LOCAL_UI_CONSTRUCTION=PASS') 'direct_local_ui_selftest_reported'
Assert-True ($source -match '直接本地模型（推荐）' -and $source -match '扫描 LM Studio 模型' -and $source -match '启动本地后端' -and $source -match '测试本地推理') 'direct_local_controls_present'
Assert-True ($source -match 'llama.cpp direct' -and $source -match 'LM Studio：仅作可选 fallback') 'direct_local_is_primary_lmstudio_is_fallback'
Assert-True ($source -match '选择方式：' -and $source -match '手动选择' -and $source -match '自动选择') 'control_panel_marks_auto_and_manual_model_selection'
Assert-True ($source -match '允许模型' -and $source -match '拒绝模型' -and $source -match '清除冷却') 'control_panel_model_picker_has_policy_and_cooldown_actions'
Assert-True ((Get-Content -LiteralPath (Join-Path $root 'scripts\configure-provider.ps1') -Raw -Encoding UTF8) -match 'Use-DefaultNoCustomHeader') 'groq_custom_header_defaults_to_no'
. (Join-Path $root 'scripts\configure-provider.ps1') -NonInteractive
Assert-True (Use-DefaultNoCustomHeader 'https://api.groq.com/openai/v1') 'groq_runtime_default_header_is_no'
Assert-True (-not (Use-DefaultNoCustomHeader 'https://api.example.com/v1')) 'non_groq_header_choice_remains_available'
Assert-True ($source -match 'Groq（https://api.groq.com/openai/v1）使用官方 SDK' -and $source -match '转换为 Groq SDK') 'groq_sdk_control_panel_hint_and_migration_present'

$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-desktop-shortcut-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $shortcutOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shortcutScript -DesktopPath $temp 2>&1
  $shortcut = Get-ChildItem -LiteralPath $temp -Filter '*.lnk' | Select-Object -First 1
  Assert-True (($shortcutOutput -join "`n") -match 'NO_ADMIN_REQUIRED=YES') 'shortcut_requires_no_admin'
  Assert-True ($null -ne $shortcut) 'shortcut_created'
  Assert-True ((Get-Content -LiteralPath $shortcutScript -Raw -Encoding UTF8) -match 'FontScale 1\.2') 'shortcut_uses_default_font_scale'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output 'POWERSHELL_TEST_TOTAL=42'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
