$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$scriptPath = Join-Path $root 'scripts\xiaoyu-router-control.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw -Encoding UTF8
$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-config-ui-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $fixtureConfig = Join-Path $temp 'config.toml'
  [IO.File]::WriteAllText($fixtureConfig, "model_provider = `"DEFAULT`"`n[model_providers.DEFAULT]`nname = `"Official`"`n", (New-Object Text.UTF8Encoding($false)))
  $outputs = @{}
  foreach ($scale in @(1.0,1.29,1.5)) {
    $selfTest = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath -SelfTest -FontScale $scale -CodexConfigPath $fixtureConfig -CodexConfigStateRoot $temp 2>&1
    $outputs[[string]$scale] = $selfTest -join "`n"
  }
  $output = $outputs['1']

Assert-True ($source -match "Codex 配置切换") 'dedicated_config_switcher_group'
Assert-True ($source.Contains("TabPage('Codex 配置切换')")) 'dedicated_config_switcher_tab'
Assert-True ($source.Contains('$tabs.Multiline = $true') -and $source.IndexOf('$assistantTab') -lt $source.IndexOf('$configSwitcherTab')) 'tab_order_and_no_overflow_arrow'
Assert-True ($source.Contains('$runtimeVersionText') -and $source.Contains('CODEX_CONFIG_SWITCHER_UI=YES')) 'runtime_version_feature_flag'
Assert-True ($source.Contains('$homeConfigSwitcherLayout') -and $source.Contains('$shortScriptPath') -and $source.Contains('AutoSizeMode')) 'responsive_layout_primitives'
foreach ($label in @('读取当前 Codex 配置','备份当前配置','捕获当前为官方配置','切到官方 Codex','切到小羽 Custom Router','恢复上一次配置','校验配置','复制重启提示')) {
  Assert-True ($source.Contains($label)) ('button_present_' + $label)
}
foreach ($action in @('status','backup','capture-official','switch-official','switch-xiaoyu','restore-previous','validate')) {
  Assert-True ($source.Contains("Invoke-CodexConfigSwitcherAction '$action'")) ('button_action_' + $action)
}
Assert-True ($source.Contains('ROUTER_NOT_LISTENING') -and $source.Contains('CODEX_CONFIG_NOT_FOUND') -and $source.Contains('OFFICIAL_PROFILE_NOT_CAPTURED')) 'specific_error_codes'
Assert-True ($source.Contains('action_name') -and $source.Contains('sanitized_reason') -and $source.Contains('suggested_fix') -and $source.Contains('router_status')) 'structured_error_fields'
Assert-True ($source.Contains('CODEX_CONFIG_SWITCHER_BUTTONS_VISIBLE=') -and $output -match 'CODEX_CONFIG_SWITCHER_BUTTONS_VISIBLE=YES') 'selftest_button_visibility'
Assert-True ($output -match 'CODEX_CONFIG_SWITCHER_BUTTON_COUNT=8') 'selftest_button_count'
Assert-True ($output -match 'CODEX_CONFIG_SWITCHER_FIXTURE_ONLY=YES') 'selftest_fixture_guard'
Assert-True ($output -match 'ROUTER_NOT_LISTENING_ERROR=PASS') 'router_not_listening_error'
Assert-True ($output -match 'CODEX_CONFIG_NOT_FOUND_ERROR=PASS') 'config_not_found_error'
Assert-True ($output -match 'LOCAL_LIGHT_UNAVAILABLE_ERROR=PASS') 'local_light_unavailable_error'
foreach ($field in @('CODEX_CONFIG_SWITCHER_TAB_VISIBLE','CODEX_CONFIG_SWITCHER_HOME_SECTION_VISIBLE','READ_CONFIG_BUTTON_VISIBLE','BACKUP_CONFIG_BUTTON_VISIBLE','CAPTURE_OFFICIAL_BUTTON_VISIBLE','SWITCH_OFFICIAL_BUTTON_VISIBLE','SWITCH_XIAOYU_BUTTON_VISIBLE','RESTORE_PREVIOUS_BUTTON_VISIBLE','VALIDATE_CONFIG_BUTTON_VISIBLE','COPY_RESTART_NOTICE_BUTTON_VISIBLE')) {
  Assert-True ($output -match ($field + '=YES')) ('ui_selfcheck_' + $field)
}
foreach ($scale in @('1','1.29','1.5')) {
  $scaledOutput = $outputs[$scale]
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_LAYOUT_NO_OVERLAP=PASS') ('layout_no_overlap_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_BUTTON_TEXT_FITS=PASS') ('button_text_fits_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_SCROLLING=PASS') ('scrolling_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_RUNTIME_PATH_SHORTENED=PASS') ('short_path_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_RUNTIME_SELFTEST=PASS') ('runtime_selftest_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_RUNTIME_COMMAND_KIND=python_module') ('runtime_command_kind_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_RUNTIME_PYTHONPATH_SET=YES') ('runtime_pythonpath_' + $scale)
  Assert-True ($scaledOutput -match 'CONFIG_SWITCHER_RUNTIME_WORKDIR_SET=YES') ('runtime_workdir_' + $scale)
}
} finally {
  if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}

Write-Output ('POWERSHELL_TEST_TOTAL=' + ($passed + $failed))
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
