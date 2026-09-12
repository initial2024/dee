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
Assert-True ($text -match 'DEEPSEEK_LOCAL_BRIDGE_PANEL_VISIBLE=YES') 'desktop_control_deepseek_local_bridge_panel_visible'
Assert-True ($text -match 'CODEX_MODE_PANEL_VISIBLE=YES' -and $text -match 'PROVIDER_ALLOWLIST_PANEL_VISIBLE=YES' -and $text -match 'LOCAL_RECORDS_PANEL_VISIBLE=YES') 'desktop_control_codex_mode_allowlist_and_records_panels_visible'
Assert-True ($text -match 'LOCAL_AGENT_PANEL_VISIBLE=YES' -and $text -match 'LOCAL_AGENT_DEFAULT_READ_ONLY=YES' -and $text -match 'LOCAL_AGENT_CONFIRMATION_GATES=YES' -and $text -match 'LOCAL_AGENT_BRAIN_EXPLICIT=YES') 'desktop_control_local_agent_panel_and_safe_defaults_visible'
Assert-True ($text -match 'DEEPSEEK_BRIDGE_DIRECT_BRAIN_UI_VISIBLE=YES' -and $text -match 'DEEPSEEK_BRIDGE_DIRECT_ENDPOINT=127\.0\.0\.1:8791') 'desktop_control_direct_deepseek_brain_visible'
Assert-True ($text -match 'OFFICIAL_ASSISTED_COORDINATOR_VISIBLE=YES' -and $text -match 'ASSIST_COORDINATE_API_VISIBLE=YES' -and $text -match '127\.0\.0\.1:18789/assist/coordinate') 'official_assisted_coordinator_panel_visible'
Assert-True ($text -match 'RESPONSE_COMPAT_DIAGNOSTICS_VISIBLE=YES') 'desktop_control_response_compat_diagnostics_visible'
Assert-True ($text -match 'TOOLS_POLICY_UI_VISIBLE=YES' -and $text -match 'TEXT_ONLY_DEFAULT_STRICT_REJECT=YES') 'desktop_control_tools_policy_visible_and_safe_default'
Assert-True ($text -match 'DEEPSEEK_HEALTH_PROMPT_SENT=NO') 'desktop_control_deepseek_health_never_sends_prompt'
Assert-True ($text -match 'CONTROL_PANEL_EXCEPTION_GUARD=YES' -and $text -match 'NO_JIT_DIALOG_ON_BUTTON_ERROR=YES') 'desktop_control_has_safe_exception_guard'
Assert-True ($text -match 'SECRET_VALUES_VISIBLE=NO') 'desktop_control_hides_secret_values'
Assert-True ($text -match 'CONTROL_PANEL_LAYOUT_POLISH=YES' -and $text -match 'LOCAL_AGENT_GROUPS=YES' -and $text -match 'OFFICIAL_ASSISTED_GROUPS=YES') 'desktop_control_grouped_layout_flags'
Assert-True ($text -match 'DEEPSEEK_HEAD_COLLABORATION_PANEL_VISIBLE=YES' -and $text -match 'DEEPSEEK_HEAD_CONTEXT_READONLY=YES' -and $text -match 'DEEPSEEK_HEAD_AUTO_BRAIN_SELECTION=YES') 'deepseek_head_collaboration_panel_flags'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'PATCH_DRAFT_FORMAT_ENFORCEMENT=YES') 'deepseek_head_patch_draft_format_flags'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'STRUCTURED_PATCH_UI=YES' -and (Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match '已由结构化补丁草案生成 unified diff，尚未应用。') 'structured_patch_ui_status_visible'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'RETRY_PROMPT_UI_EXPOSED=NO' -and (Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'credential_field_redacted') 'retry_prompt_and_sensitive_field_ui_sanitization_visible'
Assert-True ($text -match 'BUTTON_TEXT_VISIBLE=YES' -and $text -match 'WINDOW_RESIZE_SUPPORTED=YES' -and $text -match 'VERTICAL_SCROLL_SUPPORTED=YES') 'desktop_control_resize_and_scroll_flags'
Assert-True ($text -match 'DANGEROUS_ACTIONS_STILL_CONFIRM=YES' -and $text -match 'DANGEROUS_ACTION_TOOLTIPS=YES' -and $text -match 'RAW_JSON_COLLAPSED=YES') 'desktop_control_danger_and_json_flags'
  $ui = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $control -SelfTest 2>&1
Assert-True (($ui -join "`n") -match 'CONTROL_UI_INITIALIZATION=PASS') 'desktop_control_initializes_without_home_variable_error'
Assert-True (($ui -join "`n") -match 'UI_SAFE_ACTION_EXCEPTION=CAUGHT') 'desktop_control_button_exception_is_caught'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match '本控制台不会伪造额度') 'official_quota_not_faked'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match 'Router 直连测试') 'safe_router_smoke_button_present'
Assert-True ((Get-Content -LiteralPath $control -Raw -Encoding UTF8) -match '使用 OpenAI Luna（低）') 'desktop_control_exposes_handoff_openai_profile'
$source = Get-Content -LiteralPath $control -Raw -Encoding UTF8
Assert-True ($source -match '小羽 Router 控制台' -and $source -match '使用小羽 Router' -and $source -match '使用 OpenAI Luna（低）') 'chinese_title_and_buttons_present'
Assert-True ($source -match "Microsoft YaHei UI" -and $source -match '\[double\]\$FontScale = 1\.25') 'large_font_and_scale_parameter_present'
Assert-True ($source -match 'AutoScaleMode.*Dpi') 'dpi_scaling_is_enabled'
Assert-True ($source -match 'Router：\{0\}`r`n监听状态：\{1\}`r`n监听地址：\{2\}') 'status_fields_are_line_separated'
Assert-True ($source -match 'Get-RouterListenerInfo' -and $source -match '127\.0\.0\.1:18789' -and $source -match 'ROUTER_PORT_IN_USE_UNKNOWN_PROCESS') 'router_listener_is_loopback_and_unknown_port_is_protected'
Assert-True ($source -match 'Get-ProcessMetadata' -and $source -match 'executable_path' -and $source -match 'command_line') 'router_owner_metadata_is_collected'
Assert-True ($source -match 'codex[_-]ai[_-]router' -and $source -match 'xiaoyu[-_]router' -and $source -match 'owner_kind') 'router_owner_identity_matching_is_present'
Assert-True ($source -match 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' -and $source -match '未知进程占用' -and $source -match 'post_to_unknown_service') 'unknown_listener_is_safe_stopped_and_post_blocked'
Assert-True ($source -match 'STALE_OR_INCOMPATIBLE_ROUTER' -and $source -match '检测到旧小羽 Router 正在占用 18789' -and $source -match 'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED') 'stale_router_confirmation_gate_is_present'
Assert-True ($source -match "'/agent/health'" -and $source -match "'/health'" -and $source -match "'/v1/models'") 'router_identity_probe_uses_get_only_surfaces'
Assert-True ($source -match 'function Invoke-RouterDirectSmoke' -and $source -match 'preflight' -and $source -match "post_sent = 'NO'") 'router_smoke_blocks_unknown_listener_before_post'
Assert-True ($source -match '停止旧小羽 Router（需确认）') 'stop_old_router_button_is_visible'
Assert-True ($source -match 'Get-RouterLaunchSpec' -and $source -match 'codex_ai_router\.cli' -and $source -match 'ROUTER_START_TIMEOUT') 'router_one_click_start_has_python_fallback_and_timeout'
Assert-True ($source -match "'--patch-draft'" -and $source -match 'patch_draft_created' -and $source -match 'DeepSeekHeadApplyButton.Visible') 'patch_draft_ui_requires_valid_diff_before_apply'
Assert-True ($source -match '检查 18789' -and $source -match '查看 /v1/models' -and $source -match '工具策略') 'router_status_and_models_buttons_are_visible'
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
Assert-True (($ui -join "`n") -match 'ASSIST_COORDINATOR_UI_CONSTRUCTION=PASS' -and ($ui -join "`n") -match 'CODEX_ENDPOINT_TOUCHED=NO') 'assist_coordinator_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'LOCAL_REPAIR_UI_CONSTRUCTION=PASS') 'local_repair_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'DEEPSEEK_LOCAL_BRIDGE_UI_CONSTRUCTION=PASS') 'deepseek_local_bridge_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'BRIDGE_LAUNCHER_ROUTER_ROOT_RESOLVED=YES' -and ($ui -join "`n") -match 'BRIDGE_LAUNCHER_BRIDGE_ROOT_RESOLVED=YES' -and ($ui -join "`n") -match 'BRIDGE_LAUNCHER_ENTRY_EXISTS=YES') 'deepseek_launcher_roots_and_entry_selftest_reported'
Assert-True (($ui -join "`n") -match 'BRIDGE_LAUNCHER_WORKER_REQUIRED=NO' -and ($ui -join "`n") -match 'THREE_IN_ONE_PROBE_NO_WORKER_REQUIRED=YES') 'deepseek_launcher_bridge_only_selftest_reported'
Assert-True (($ui -join "`n") -match 'DEEPSEEK_HEAD_UI_CONSTRUCTION=PASS' -and ($ui -join "`n") -match 'DEEPSEEK_HEAD_CONTEXT_REDACTION=PASS') 'deepseek_head_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'TOOLS_POLICY_UI_CONSTRUCTION=PASS') 'tools_policy_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'LOCAL_AGENT_UI_CONSTRUCTION=PASS' -and ($ui -join "`n") -match 'LOCAL_AGENT_CONFIRMATION_GATES=PASS') 'local_agent_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'LOCAL_AGENT_GROUP_LAYOUT=PASS' -and ($ui -join "`n") -match 'OFFICIAL_ASSISTED_GROUP_LAYOUT=PASS') 'grouped_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'BUTTON_TEXT_LAYOUT=PASS' -and ($ui -join "`n") -match 'WINDOW_RESIZE_LAYOUT=PASS' -and ($ui -join "`n") -match 'VERTICAL_SCROLL_LAYOUT=PASS') 'responsive_ui_selftest_reported'
Assert-True (($ui -join "`n") -match 'DANGEROUS_ACTION_TOOLTIPS=PASS' -and ($ui -join "`n") -match 'RAW_JSON_COLLAPSED=PASS') 'dangerous_tooltip_and_json_selftest_reported'
Assert-True (($ui -join "`n") -match 'SESSION_BINDING_UI_CONSTRUCTION=PASS' -and ($ui -join "`n") -match 'SESSION_BINDING_LOCAL_ONLY=YES' -and ($ui -join "`n") -match 'SESSION_BINDING_WEB_SESSION_READ=NO') 'session_binding_ui_selftest_reported'
Assert-True ($source -match '直接本地模型（推荐）' -and $source -match '扫描 LM Studio 模型' -and $source -match '启动本地后端' -and $source -match '测试本地推理') 'direct_local_controls_present'
Assert-True ($source -match '修复本地后端' -and $source -match "local @Arguments") 'direct_local_repair_control_present'
Assert-True ($source -match 'llama.cpp direct' -and $source -match 'LM Studio：仅作可选 fallback') 'direct_local_is_primary_lmstudio_is_fallback'
Assert-True ($source -match '选择方式：' -and $source -match '手动选择' -and $source -match '自动选择') 'control_panel_marks_auto_and_manual_model_selection'
Assert-True ($source -match '允许模型' -and $source -match '拒绝模型' -and $source -match '清除冷却') 'control_panel_model_picker_has_policy_and_cooldown_actions'
Assert-True ($source -match 'DeepSeek 本地桥接' -and $source -match 'LOCAL_DIRECT' -and $source -match 'http://127.0.0.1:8791/v1') 'deepseek_local_bridge_status_panel_present'
Assert-True ($source -match '会话绑定' -and $source -match '新建绑定会话' -and $source -match '生成 DeepSeek 上下文包' -and $source -match '清理会话敏感缓存') 'session_binding_controls_are_chinese_and_visible'
Assert-True ($source -match '不读取 Codex 或 DeepSeek 网页私有数据' -and $source -match 'Append-CodexStatusToSession') 'session_binding_preserves_local_only_boundary'
Assert-True ($source -match 'DeepSeek 网页模式策略（只读探测）' -and $source -match '探测 DeepSeek 模式' -and $source -match 'DEEPSEEK_MODE_PROBE_UI_VISIBLE=YES') 'deepseek_read_only_mode_probe_ui_present'
Assert-True ($source -match '自动选择模式' -and $source -match '省时模式' -and $source -match '平衡模式' -and $source -match '严谨模式' -and $source -match '固定快速\+思考' -and $source -match '固定专家\+深度思考' -and $source -match '固定视觉\+专家\+深度思考' -and $source -match '固定文件提取') 'deepseek_mode_selector_buttons_present'
Assert-True ($source -match 'function Invoke-RouterModeSwitch' -and $source -match '/deepseek/mode-switch' -and $source -match '仅切换：快速' -and $source -match '仅预检：视觉\+专家\+思考' -and $source -match '仅预检：文件提取') 'deepseek_mode_switch_controls_are_loopback_only'
Assert-True ($source -match '最近推荐模式' -and $source -match 'Format-DeepSeekSelectionSummary') 'deepseek_mode_recommendation_is_shown_in_chinese'
Assert-True ($source -match 'mode-probe' -and $source -match 'promptSent' -and $source -match 'clickSend') 'deepseek_mode_probe_reports_no_prompt_or_send'
Assert-True ($source -match 'CONTROL_PANEL_JSON_POPUP_DEFAULT=NO' -and $source -match '原始 JSON：已折叠') 'codex_mode_switch_shows_chinese_summary_by_default'
Assert-True ($source -match '复制诊断 JSON' -and $source -match '脱敏诊断 JSON') 'codex_mode_debug_json_requires_explicit_copy'
Assert-True ($source -match '不适用（官方直连）' -and $source -match 'Get-CodexModeChineseName') 'official_direct_tools_policy_is_not_applicable'
Assert-True ($source -match 'OFFICIAL_DIRECT' -and $source -match 'CUSTOM_ROUTER' -and $source -match 'OFFICIAL_ASSISTED') 'codex_legacy_modes_documented'
Assert-True ($source -match 'CUSTOM_DEEPSEEK_HEAD' -and $source -match 'CUSTOM_HYBRID_AGENT') 'deepseek_head_and_hybrid_modes_present'
Assert-True ($source -match 'CUSTOM_DEEPSEEK_HEAD' -and $source -match 'CUSTOM_LOCAL_LIGHT' -and $source -match 'CUSTOM_EXTERNAL_API' -and $source -match 'CUSTOM_HYBRID_AGENT') 'a4_codex_modes_present'
Assert-True ($source -match '严格拒绝工具' -and $source -match '文本兼容：忽略工具' -and $source -match '手动计划（不调用模型）') 'text_only_tools_policy_buttons_present'
Assert-True ($source -match 'tools-policy' -and $source -match 'TEXT_ONLY 兼容模式' -and $source -match '不会执行工具或修改文件') 'text_only_tools_policy_is_explicit_and_chinese'
Assert-True ($source -match 'custom-deepseek-text-only' -and $source -match 'custom-local-text-only' -and $source -match 'custom-hybrid-text-only') 'text_only_mode_switch_actions_present'
Assert-True ($source -match 'Provider Allowlist' -and $source -match '外部 API 默认禁用' -and $source -match 'CODEX_TASK_INPUT_LOCATION=CODEX_ONLY') 'allowlist_panel_and_codex_task_boundary_present'
Assert-True ($source -match '小羽本地 Agent' -and $source -match '生成计划（PLAN_ONLY）' -and $source -match '应用补丁（双确认）' -and $source -match '提交 commit（双确认）') 'local_agent_panel_actions_are_chinese_and_gated'
Assert-True ($source -match 'Brain Provider' -and $source -match '只读与计划' -and $source -match '需确认执行' -and $source -match "Text = '记录'") 'local_agent_actions_are_grouped'
Assert-True ($source -match '官方辅助协调' -and $source -match '辅助分析' -and $source -match '辅助脑') 'official_assisted_actions_are_grouped'
Assert-True ($source -match 'DeepSeek 首脑协作' -and $source -match '自动选择辅助脑' -and $source -match '收集项目上下文' -and $source -match '发送给 DeepSeek 首脑分析') 'deepseek_head_collaboration_actions_present'
Assert-True ($source -match 'DeepSeek 首脑在发送前被阻断' -and $source -match '网页发送计数' -and $source -match '补丁草案：未尝试') 'pre_send_provider_error_is_shown_in_chinese'
Assert-True ($source -match 'DEEPSEEK_HEAD_CONTEXT_REDACTION=YES' -and $source -match '127\.0\.0\.1:8791' -and $source -match '不会发送密钥、Cookie、Token 或 Authorization') 'deepseek_head_context_redaction_and_loopback_notice_present'
Assert-True ($source -match 'AutoScroll = \$true' -and $source -match 'WrapContents = \$true' -and $source -match 'AutoEllipsis=\$false') 'grouped_actions_support_scroll_and_full_text'
Assert-True ($source -match '需要确认，不自动执行' -and $source -match '需要确认；只运行白名单测试' -and $source -match '需要确认，不 push' -and $source -match '只停止本地 Agent，不影响系统') 'dangerous_actions_have_chinese_tooltips'
Assert-True ($source -match "Items.Add\('自动'\)" -and $source -match "Items.Add\('本地模型'\)" -and $source -match "Items.Add\('DeepSeek Bridge 直连" -and $source -match "Items.Add\('外部 API'\)" -and $source -match "Items.Add\('Hybrid'\)") 'brain_provider_choices_are_complete'
Assert-True ($source -match '调用大脑生成计划' -and $source -match '--invoke-brain' -and $source -match '.codex-ai-router\\local-agent') 'local_agent_explicit_brain_button_and_storage_path'
Assert-True ($source -match 'deepseek-bridge-direct' -and $source -match 'DeepSeek Bridge 直连（本地 8791）' -and $source -match '127\.0\.0\.1:8791') 'direct_deepseek_brain_provider_selector_is_chinese_and_loopback'
Assert-True ($source -match '自动修改：NO' -and $source -match '自动 push：NO' -and $source -match '不会自动修改文件、运行测试或提交') 'local_agent_no_automatic_high_risk_execution'
Assert-True ($source -notmatch '\$lunaTask' -and $source -notmatch 'DeepSeek 首脑 / Luna Agent') 'xiaoyu_console_has_no_primary_task_input'
Assert-True ($source -match 'Invoke-ModelsOnlyDiagnostic' -and $source -notmatch 'Invoke-ModelsOnlyDiagnostic.*chat/completions') 'models_diagnostic_has_no_chat_prompt'
Assert-True ($source -match 'Response Compatibility' -or (Test-Path -LiteralPath (Join-Path $root 'src\codex_ai_router\response_compat.py'))) 'response_normalizer_is_present'
Assert-True ($source -match 'Resolve-BridgeRoot' -and $source -match 'Resolve-BridgeStartCommand' -and $source -match 'deepseek-web-browser-bridge-poc' -and $source -match 'npm start') 'deepseek_local_bridge_uses_resolved_bridge_entrypoint'
Assert-True ($source -match '健康检查只读取本地服务与页面状态，不发送 prompt' -and $source -match 'DEEPSEEK_HEALTH_PROMPT_SENT=NO' -and $source -match 'Resolve-BridgeHealthCommand') 'deepseek_health_is_non_generating'
Assert-True ($source -match 'BRIDGE_ONLY_HEALTH_NO_WORKER' -and $source -match 'THREE_IN_ONE_PROBE_NO_WORKER_REQUIRED' -and $source -match 'WORKER_SCRIPT_NOT_REQUIRED_FOR_DIRECT_BRIDGE') 'deepseek_bridge_only_health_does_not_require_worker'
Assert-True ($source -match '第一次确认' -and $source -match '第二次确认' -and $source -match '会发送一次真实 DeepSeek 测试对话') 'deepseek_smoke_requires_double_confirmation'
Assert-True ($source -match '不会显示或保存 prompt、response、key、Cookie 或 Token' -and $source -match 'Add-DeepSeekLog') 'deepseek_panel_log_is_sanitized'
Assert-True ($source -notmatch '/api/v0/chat/completion' -and $source -notmatch 'x-ds-pow-response' -and $source -notmatch 'storageState') 'deepseek_panel_has_no_private_api_or_browser_state_export'
Assert-True ($source -match '检查启动器路径' -and $source -match '复制启动器诊断' -and $source -match '打开 Bridge 项目目录' -and $source -match 'ERROR_DETAILS_SANITIZED=YES') 'deepseek_launcher_diagnostics_ui_present'
Assert-True ($source -match 'BRIDGE_ROOT_NOT_FOUND' -and $source -match 'BRIDGE_START_ENTRY_NOT_FOUND' -and $source -match 'BRIDGE_RUNTIME_NOT_FOUND' -and $source -match 'SCRIPT_NOT_FOUND_SANITIZED') 'deepseek_launcher_specific_errors_present'
Assert-True ($source -match 'open-deepseek-web' -and $source -match 'Start-Process \$DeepSeekWebUrl') 'deepseek_open_web_button_is_browser_only'
Assert-True ((Get-Content -LiteralPath (Join-Path $root 'scripts\configure-provider.ps1') -Raw -Encoding UTF8) -match 'Use-DefaultNoCustomHeader') 'groq_custom_header_defaults_to_no'
. (Join-Path $root 'scripts\configure-provider.ps1') -NonInteractive
Assert-True (Use-DefaultNoCustomHeader 'https://api.groq.com/openai/v1') 'groq_runtime_default_header_is_no'
Assert-True (-not (Use-DefaultNoCustomHeader 'https://api.example.com/v1')) 'non_groq_header_choice_remains_available'
Assert-True ($source -match 'Groq（https://api.groq.com/openai/v1）使用官方 SDK' -and $source -match '转换为 Groq SDK') 'groq_sdk_control_panel_hint_and_migration_present'
Assert-True ($source -match 'Invoke-SafeUiAction' -and $source -match '操作失败' -and $source -match '高级信息 / 调试信息') 'control_panel_uses_safe_chinese_error_summary'
Assert-True ($source -match 'Groq 诊断' -and $source -match 'Groq 手动真实测试' -and $source -match 'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED') 'groq_diagnostics_and_allowlist_gate_visible'

$modeRoot = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-mode-' + [guid]::NewGuid().ToString())
try {
  New-Item -ItemType Directory -Path $modeRoot | Out-Null
  $modeConfig = Join-Path $modeRoot 'config.toml'
  [IO.File]::WriteAllText($modeConfig, "model = `"official-model`"`nmodel_reasoning_effort = `"medium`"`n", (New-Object Text.UTF8Encoding($false)))
  $modeManager = Join-Path $root 'scripts\codex-mode-manager.ps1'
  $custom = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-router -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($custom.mode -eq 'CUSTOM_ROUTER' -and $custom.endpoint -eq 'LOCAL_8792') 'custom_router_sets_loopback_only_config'
  $headCustom = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-deepseek-head -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($headCustom.mode -eq 'CUSTOM_DEEPSEEK_HEAD' -and $headCustom.model -eq 'deepseek-head' -and $headCustom.endpoint -eq 'LOCAL_8792') 'custom_deepseek_head_sets_loopback_config'
  $localCustom = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-local-light -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($localCustom.mode -eq 'CUSTOM_LOCAL_LIGHT' -and $localCustom.model -eq 'local-light' -and $localCustom.endpoint -eq 'LOCAL_MODEL_1234') 'custom_local_light_sets_local_config'
  $externalCustom = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-external-api -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($externalCustom.mode -eq 'CUSTOM_EXTERNAL_API' -and $externalCustom.model -eq 'external-fast' -and $externalCustom.endpoint -eq 'LOCAL_ROUTER_18789') 'custom_external_api_uses_local_router'
  $hybridCustom = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-hybrid-agent -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($hybridCustom.mode -eq 'CUSTOM_HYBRID_AGENT' -and $hybridCustom.model -eq 'hybrid-agent' -and $hybridCustom.endpoint -eq 'LOCAL_ROUTER_18789') 'custom_hybrid_uses_local_router'
  $deepseekText = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-deepseek-text-only -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($deepseekText.mode -eq 'CUSTOM_DEEPSEEK_TEXT_ONLY' -and $deepseekText.model -eq 'deepseek-web' -and $deepseekText.endpoint -eq 'LOCAL_ROUTER_18789') 'custom_deepseek_text_only_uses_router'
  $localText = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-local-text-only -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($localText.mode -eq 'CUSTOM_LOCAL_TEXT_ONLY' -and $localText.model -eq 'local-light' -and $localText.endpoint -eq 'LOCAL_ROUTER_18789') 'custom_local_text_only_uses_router'
  $hybridText = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action custom-hybrid-text-only -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($hybridText.mode -eq 'CUSTOM_HYBRID_TEXT_ONLY' -and $hybridText.model -eq 'hybrid-agent' -and $hybridText.endpoint -eq 'LOCAL_ROUTER_18789') 'custom_hybrid_text_only_uses_router'
  $assisted = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action official-assisted -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($assisted.mode -eq 'OFFICIAL_ASSISTED' -and $assisted.env_mutation -eq 'NONE') 'official_assisted_preserves_endpoint_environment'
  $assistedCoordinator = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action official-assisted-coordinator -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($assistedCoordinator.mode -eq 'OFFICIAL_ASSISTED_COORDINATOR' -and $assistedCoordinator.env_mutation -eq 'NONE' -and $assistedCoordinator.endpoint -eq $hybridText.endpoint) 'official_assisted_coordinator_preserves_endpoint_environment'
  $head = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action deepseek-head -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($head.mode -eq 'DEEPSEEK_HEAD' -and $head.env_mutation -eq 'NONE') 'deepseek_head_mode_does_not_change_endpoint_or_environment'
  $official = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $modeManager -Action official-direct -ConfigPath $modeConfig -StateRoot (Join-Path $modeRoot 'state') 2>&1 | Out-String | ConvertFrom-Json
  Assert-True ($official.mode -eq 'OFFICIAL_DIRECT' -and ((Get-Content -LiteralPath $modeConfig -Raw -Encoding UTF8) -match 'official-model')) 'official_mode_restores_baseline'
  Assert-True ((Get-ChildItem -LiteralPath (Join-Path $modeRoot 'state') -Filter 'config-*.toml').Count -ge 2) 'mode_switch_creates_timestamped_backups'
} finally { if (Test-Path -LiteralPath $modeRoot) { Remove-Item -LiteralPath $modeRoot -Recurse -Force } }

$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-desktop-shortcut-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $shortcutOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $shortcutScript -DesktopPath $temp 2>&1
  $shortcut = Get-ChildItem -LiteralPath $temp -Filter '*.lnk' | Select-Object -First 1
  Assert-True (($shortcutOutput -join "`n") -match 'NO_ADMIN_REQUIRED=YES') 'shortcut_requires_no_admin'
  Assert-True ($null -ne $shortcut) 'shortcut_created'
  Assert-True ((Get-Content -LiteralPath $shortcutScript -Raw -Encoding UTF8) -match 'FontScale 1\.2') 'shortcut_uses_default_font_scale'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output ('POWERSHELL_TEST_TOTAL=' + ($passed + $failed))
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
