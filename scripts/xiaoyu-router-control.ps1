[CmdletBinding()]
param(
    [switch]$NoShow,
    [switch]$SelfTest,
    [ValidateRange(0.8, 2.0)]
    [double]$FontScale = 1.15,
    [string]$ProviderConfigPath = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProviderConfig = if (-not [string]::IsNullOrWhiteSpace($ProviderConfigPath)) { $ProviderConfigPath } elseif ($env:XIAOYU_ROUTER_PROVIDER_CONFIG) { $env:XIAOYU_ROUTER_PROVIDER_CONFIG } else { Join-Path $env:USERPROFILE '.codex-ai-router\providers.json' }
$RuntimeConfig = Join-Path $env:USERPROFILE '.codex-ai-router\runtime-models.json'
$UsageLedger = Join-Path $env:USERPROFILE '.codex-ai-router\usage-ledger.jsonl'
$CodexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'

function Read-JsonFile([string]$Path, [object]$Fallback) {
    try { if (Test-Path -LiteralPath $Path) { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } } catch {}
    return $Fallback
}
function Write-JsonAtomic([string]$Path, [object]$Value) {
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $temp = Join-Path $directory ('.providers-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($temp, ($Value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}
function Redact-Text([string]$Text) { return ($Text -replace '(?i)(bearer\s+)[^\s]+','$1[REDACTED]' -replace '(?i)(sk-[a-z0-9_-]+)','[REDACTED]' -replace '(?i)(authorization\s*[:=]\s*)[^\s,;]+','$1[REDACTED]') }
function Get-ProviderData {
    if (-not (Test-Path -LiteralPath $ProviderConfig)) { return [pscustomobject]@{ providers = [pscustomobject]@{} } }
    try { return (Get-Content -LiteralPath $ProviderConfig -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { throw ('供应商元数据解析失败：' + $_.Exception.Message) }
}
function Get-ProviderRows {
    $data = Get-ProviderData; $runtime = Read-JsonFile $RuntimeConfig ([pscustomobject]@{ providers = [pscustomobject]@{} }); $rows = @()
    foreach ($property in @($data.providers.psobject.Properties)) {
        $provider = $property.Value; $state = $runtime.providers.($property.Name); $selected = $null; $last = if($provider.last_discovery_status){[string]$provider.last_discovery_status}else{'UNKNOWN'}
        if ($state) { $passing = @($state.psobject.Properties | Where-Object { $_.Value.status -eq 'PASS' } | Sort-Object { $_.Value.elapsed_seconds }); if ($passing.Count -gt 0) { $selected = $passing[0].Name }; if($provider.preferred_runtime_model -and @($passing.Name) -contains $provider.preferred_runtime_model){$selected=$provider.preferred_runtime_model}; $latest = @($state.psobject.Properties | Sort-Object { $_.Value.checked_at } -Descending | Select-Object -First 1); if ($latest.Count -gt 0) { $last = [string]$latest[0].Value.status } }
        $registry = $provider.model_registry; $discovered = if($registry -and $registry.DISCOVERED_MODELS){@($registry.DISCOVERED_MODELS)}elseif($provider.last_discovery_models){@($provider.last_discovery_models)}else{@($provider.models)}; $seeds=@($provider.allowed_model_seeds); $usable = if($registry -and $null -ne $registry.USABLE_MODELS){@($registry.USABLE_MODELS)}elseif($seeds.Count -gt 0){@($discovered | Where-Object { $_ -in $seeds })}else{$discovered}
        $rows += [pscustomobject]@{ provider_id = $property.Name; display_name = if ($provider.display_name) { $provider.display_name } else { $property.Name }; provider_type = $provider.type; base_url = $provider.base_url; wire_api = $provider.wire_api; enabled = if ($provider.enabled -eq $false) { 'NO' } else { 'YES' }; discovered_model_count = $discovered.Count; usable_model_count = $usable.Count; last_runtime_status = $last; selected_runtime_model = $selected }
    }
    return $rows
}
function Get-CodexStatus {
    $provider = 'DEFAULT'; $model = 'DEFAULT'; $xiaoyu = $false
    if (Test-Path -LiteralPath $CodexConfig) { $lines = Get-Content -LiteralPath $CodexConfig -Encoding UTF8; foreach ($line in $lines) { if ($line -match '^\s*\[') { break }; if ($line -match '^\s*model_provider\s*=\s*"([^"]*)"') { $provider = $Matches[1] }; if ($line -match '^\s*model\s*=\s*"([^"]*)"') { $model = $Matches[1] } }; $xiaoyu = ([string]::Join("`n", $lines) -match '\[model_providers\.XiaoyuRouter\]') }
    return [pscustomobject]@{ provider = $provider; model = $model; xiaoyu = $xiaoyu }
}
function Get-RouterStatus {
    try { $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18789/health' -TimeoutSec 2; $models = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18789/v1/models' -TimeoutSec 2 | ConvertFrom-Json; return [pscustomobject]@{ running = ($health.StatusCode -eq 200); address = 'http://127.0.0.1:18789/v1'; mode = 'AUTO'; models = $models } } catch { return [pscustomobject]@{ running = $false; address = 'http://127.0.0.1:18789/v1'; mode = 'AUTO'; models = $null } }
}
function Start-Router { if ((Get-RouterStatus).running) { return }; $command = Get-Command xiaoyu-router -CommandType Application | Select-Object -First 1; if (-not $command) { throw 'xiaoyu-router command was not found.' }; Start-Process -FilePath $command.Path -ArgumentList 'serve --port 18789' -WindowStyle Hidden }
function Stop-Router { $listener = Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($listener) { Stop-Process -Id $listener.OwningProcess -Force } }
function Invoke-RouterDirectSmoke {
    $body = @{ model = 'xiaoyu-lightboat'; input = 'Reply exactly: ROUTER_SMOKE_OK'; max_output_tokens = 8; stream = $false } | ConvertTo-Json -Compress
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $result = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18789/v1/responses' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 25
        return [pscustomobject]@{ success = ($result.StatusCode -eq 200); status = $result.StatusCode; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2); error_code = $null }
    } catch {
        return [pscustomobject]@{ success = $false; status = 'FAILED'; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2); error_code = 'ROUTER_SMOKE_FAILED' }
    }
}
function Update-ProviderEnabled([string]$Id) { $data = Get-ProviderData; $provider = $data.providers.($Id); if (-not $provider) { throw 'Provider was not found.' }; $provider.enabled = ($provider.enabled -eq $false); Write-JsonAtomic $ProviderConfig $data }
function Invoke-RouterCli([string[]]$Arguments) { return (Redact-Text (& xiaoyu-router @Arguments 2>&1 | Out-String)) }
function Add-UsageRecord([hashtable]$Record) {
    $allowed = @('timestamp','active_provider','active_model','router_virtual_model','task_mode','duration_seconds','success','error_code','estimated_route','remote_provider_used','local_provider_used'); $safe = @{}
    foreach ($key in $allowed) { if ($Record.ContainsKey($key)) { $safe[$key] = $Record[$key] } }; $safe.timestamp = (Get-Date).ToUniversalTime().ToString('o'); $directory = Split-Path -Parent $UsageLedger
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }; [IO.File]::AppendAllText($UsageLedger, ((Redact-Text ($safe | ConvertTo-Json -Compress)) + "`n"), (New-Object Text.UTF8Encoding($false)))
}
function Get-UsageSummary {
    $summary = @{ today = 0; week = 0; openai = 0; xiaoyu = 0; lightboat = 0; local = 0; failed = 0 }; if (-not (Test-Path -LiteralPath $UsageLedger)) { return $summary }; $today = (Get-Date).ToUniversalTime().Date; $weekStart = (Get-Date).ToUniversalTime().AddDays(-7)
    foreach ($line in Get-Content -LiteralPath $UsageLedger -Encoding UTF8) { try { $entry = $line | ConvertFrom-Json; $at = ([datetime]$entry.timestamp).ToUniversalTime(); if ($at.Date -eq $today) { $summary.today++ }; if ($at -lt $weekStart) { continue }; $summary.week++; if ($entry.active_provider -in @('OpenAI','DEFAULT')) { $summary.openai++ }; if ($entry.active_provider -eq 'XiaoyuRouter') { $summary.xiaoyu++ }; if (("$($entry.router_virtual_model)$($entry.estimated_route)") -match '(?i)lightboat') { $summary.lightboat++ }; if ($entry.local_provider_used -eq 'YES') { $summary.local++ }; if (-not $entry.success -or "$($entry.error_code)" -match 'TIMEOUT') { $summary.failed++ } } catch {} }
    return $summary
}

if ($NoShow) {
    $router = Get-RouterStatus; $rows = Get-ProviderRows
    Write-Output ('ROUTER_STATUS_VISIBLE=' + $(if ($router.running) { 'YES' } else { 'NO' })); Write-Output 'CODEX_STATUS_VISIBLE=YES'; Write-Output 'PROVIDER_LIST_VISIBLE=YES'; Write-Output ('LIGHTBOAT_PROVIDER_VISIBLE=' + $(if (@($rows | Where-Object { $_.provider_id -eq 'lightboat-3' }).Count -gt 0) { 'YES' } else { 'NO' })); Write-Output 'USAGE_GUARD_VISIBLE=YES'; Write-Output 'SECRET_VALUES_VISIBLE=NO'; exit 0
}

$uiFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$buttonFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$form = New-Object System.Windows.Forms.Form; $form.Text = '小羽 Router 控制台'; $form.Size = New-Object System.Drawing.Size(1180,780); $form.MinimumSize = New-Object System.Drawing.Size(920,620); $form.StartPosition = 'CenterScreen'; $form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi; $form.Font = $uiFont
$tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = 'Fill'; $form.Controls.Add($tabs)
$homeTab = New-Object System.Windows.Forms.TabPage('首页'); $providersTab = New-Object System.Windows.Forms.TabPage('供应商'); $usageTab = New-Object System.Windows.Forms.TabPage('用量保护'); $diagnosticsTab = New-Object System.Windows.Forms.TabPage('诊断'); [void]$tabs.TabPages.AddRange(@($homeTab,$providersTab,$usageTab,$diagnosticsTab))
$homeLayout = New-Object System.Windows.Forms.TableLayoutPanel; $homeLayout.Dock = 'Fill'; $homeLayout.Padding = New-Object System.Windows.Forms.Padding(12); $homeLayout.RowCount = 3; $homeLayout.ColumnCount = 1; [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $homeTab.Controls.Add($homeLayout)
$routerGroup = New-Object System.Windows.Forms.GroupBox; $routerGroup.Text = 'Router 服务'; $routerGroup.Dock = 'Fill'; $routerGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($routerGroup,0,0)
$homeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $homeButtons.Dock = 'Fill'; $homeButtons.AutoSize = $true; $routerGroup.Controls.Add($homeButtons)
$switchGroup = New-Object System.Windows.Forms.GroupBox; $switchGroup.Text = 'Codex 切换与交接'; $switchGroup.Dock = 'Fill'; $switchGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($switchGroup,0,1)
$switchButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $switchButtons.Dock = 'Fill'; $switchButtons.AutoSize = $true; $switchGroup.Controls.Add($switchButtons)
$statusGroup = New-Object System.Windows.Forms.GroupBox; $statusGroup.Text = '当前状态'; $statusGroup.Dock = 'Fill'; $statusGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($statusGroup,0,2)
$statusBox = New-Object System.Windows.Forms.TextBox; $statusBox.Multiline = $true; $statusBox.ReadOnly = $true; $statusBox.Dock = 'Fill'; $statusBox.ScrollBars = 'Vertical'; $statusBox.Font = $uiFont; $statusGroup.Controls.Add($statusBox)
$providerLayout = New-Object System.Windows.Forms.TableLayoutPanel; $providerLayout.Dock = 'Fill'; $providerLayout.Padding = New-Object System.Windows.Forms.Padding(12); $providerLayout.RowCount = 4; $providerLayout.ColumnCount = 1; [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,130))); $providersTab.Controls.Add($providerLayout)
$providerButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $providerButtons.Dock = 'Fill'; $providerButtons.AutoSize = $true; $providerLayout.Controls.Add($providerButtons,0,0)
$providerStatus = New-Object System.Windows.Forms.Label; $providerStatus.Dock = 'Fill'; $providerStatus.AutoSize = $true; $providerStatus.Padding = New-Object System.Windows.Forms.Padding(4); $providerStatus.Font = $uiFont; $providerLayout.Controls.Add($providerStatus,0,1)
$grid = New-Object System.Windows.Forms.DataGridView; $grid.Dock = 'Fill'; $grid.ReadOnly = $true; $grid.Font = $uiFont; $grid.AutoGenerateColumns = $true; $grid.AutoSizeColumnsMode = 'Fill'; $grid.SelectionMode = 'FullRowSelect'; $grid.MultiSelect = $false; $grid.AllowUserToAddRows = $false; $grid.AllowUserToDeleteRows = $false; $providerLayout.Controls.Add($grid,0,2)
$providerLogGroup = New-Object System.Windows.Forms.GroupBox; $providerLogGroup.Text = '最近操作日志（已脱敏）'; $providerLogGroup.Dock = 'Fill'; $providerLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $providerLayout.Controls.Add($providerLogGroup,0,3)
$providerLog = New-Object System.Windows.Forms.TextBox; $providerLog.Multiline = $true; $providerLog.ReadOnly = $true; $providerLog.ScrollBars = 'Vertical'; $providerLog.Font = $uiFont; $providerLog.Dock = 'Fill'; $providerLogGroup.Controls.Add($providerLog)
$usageText = New-Object System.Windows.Forms.TextBox; $usageText.Multiline = $true; $usageText.ReadOnly = $true; $usageText.Font = $uiFont; $usageText.Dock = 'Fill'; $usageTab.Controls.Add($usageText)
$usageButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $usageButtons.Dock = 'Top'; $usageButtons.Height = 42; $usageTab.Controls.Add($usageButtons)
$diagnosticsText = New-Object System.Windows.Forms.TextBox; $diagnosticsText.Multiline = $true; $diagnosticsText.ReadOnly = $true; $diagnosticsText.Font = $uiFont; $diagnosticsText.Dock = 'Fill'; $diagnosticsTab.Controls.Add($diagnosticsText)
function Add-ProviderLog([string]$Message) { $line = ('[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), (Redact-Text $Message)); $providerLog.AppendText($line + "`r`n") }
function Format-ModelList([object]$Models) { $items=@($Models); if($items.Count -eq 0){return 'NONE'}; $shown=@($items|Select-Object -First 8) -join ', '; if($items.Count -gt 8){return ($shown + (' …（共 {0} 个）' -f $items.Count))}; return $shown }
function New-ProviderTable {
    $table = New-Object System.Data.DataTable
    foreach($name in @('provider_id','显示名','类型','Base URL','wire_api','启用状态','发现模型数','可用模型数','运行状态','当前运行模型')) { [void]$table.Columns.Add($name) }
    return ,$table
}
function Refresh-Providers {
    try {
        $rows = @(Get-ProviderRows)
        $table = New-ProviderTable
        foreach($row in $rows) { [void]$table.Rows.Add($row.provider_id,$row.display_name,$row.provider_type,$row.base_url,$row.wire_api,$row.enabled,$row.discovered_model_count,$row.usable_model_count,$row.last_runtime_status,$row.selected_runtime_model) }
        $grid.DataSource = $null; $grid.DataSource = $table
        if($rows.Count -eq 0) { $providerStatus.Text = '暂无供应商，请点击“新增供应商”。'; Add-ProviderLog '刷新列表完成：没有可显示的供应商。' }
        else { $providerStatus.Text = ('供应商数量：{0}；刷新时间：{1}' -f $rows.Count,(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')); Add-ProviderLog ('刷新列表成功：读取 {0} 个供应商。' -f $rows.Count) }
    } catch {
        $summary = Redact-Text $_.Exception.Message
        $providerStatus.Text = ('读取供应商列表失败：' + $summary)
        Add-ProviderLog ('刷新列表失败：' + $summary)
        $grid.DataSource = New-ProviderTable
    }
}
function Selected-Provider { if ($grid.CurrentRow -and $grid.CurrentRow.Cells['provider_id'].Value) { return [string]$grid.CurrentRow.Cells['provider_id'].Value }; return $null }
function Require-SelectedProvider { $id = Selected-Provider; if([string]::IsNullOrWhiteSpace($id)){ [System.Windows.Forms.MessageBox]::Show('请先选择一个供应商。','供应商操作'); return $null }; return $id }
function Set-PreferredRuntimeModel([string]$Id,[string]$Model) { $data=Get-ProviderData; $provider=$data.providers.($Id); if(-not $provider){throw 'Provider was not found.'}; $allowed=@($provider.model_registry.USABLE_MODELS); if($Model -notin $allowed){throw '只能选择允许的可用模型。'}; $provider | Add-Member -NotePropertyName preferred_runtime_model -NotePropertyValue $Model -Force; Write-JsonAtomic $ProviderConfig $data }
function Open-ModelPicker([string]$Id) {
    $provider=(Get-ProviderData).providers.($Id); if(-not $provider){throw 'Provider was not found.'}; $models=@($provider.model_registry.DISCOVERED_MODELS); if($models.Count -eq 0){[System.Windows.Forms.MessageBox]::Show('暂无可选择模型。请先刷新模型并运行探测。','模型选择');return}
    $runtime=(Read-JsonFile $RuntimeConfig ([pscustomobject]@{providers=[pscustomobject]@{}})).providers.($Id); $sources=$provider.model_registry.SOURCES
    $dialog=New-Object System.Windows.Forms.Form; $dialog.Text=('模型选择：' + $Id); $dialog.Size=New-Object System.Drawing.Size(760,480); $dialog.StartPosition='CenterParent'; $dialog.Font=$uiFont; $layout=New-Object System.Windows.Forms.TableLayoutPanel; $layout.Dock='Fill';$layout.Padding=New-Object System.Windows.Forms.Padding(10);$layout.RowCount=2;$layout.ColumnCount=1;$dialog.Controls.Add($layout)
    $list=New-Object System.Windows.Forms.ListBox;$list.Dock='Fill';$list.Font=$uiFont;foreach($model in $models){$item=$runtime.($model);$status=if($item){$item.status}else{'UNKNOWN'};$latency=if($item){$item.elapsed_seconds}else{'-'};$source=if($sources.($model)){$sources.($model)}else{'UNKNOWN'};$manual=if($provider.preferred_runtime_model -eq $model){'（手动首选）'}else{''};[void]$list.Items.Add(('{0} | {1} | {2}s | {3} {4}' -f $model,$status,$latency,$source,$manual));if($provider.preferred_runtime_model -eq $model){$list.SelectedIndex=$list.Items.Count-1}};$layout.Controls.Add($list,0,0)
    $buttons=New-Object System.Windows.Forms.FlowLayoutPanel;$buttons.Dock='Fill';$layout.Controls.Add($buttons,0,1);$save=New-Object System.Windows.Forms.Button;$save.Text='设为当前/首选模型';$save.Font=$buttonFont;$save.Width=190;$save.Add_Click({if($list.SelectedIndex -lt 0){return};$model=$models[$list.SelectedIndex];Set-PreferredRuntimeModel $Id $model;[System.Windows.Forms.MessageBox]::Show(('已保存手动模型：' + $model),'模型选择');$dialog.DialogResult=[System.Windows.Forms.DialogResult]::OK;$dialog.Close()});[void]$buttons.Controls.Add($save);$allow=New-Object System.Windows.Forms.Button;$allow.Text='允许模型';$allow.Font=$buttonFont;$allow.Width=100;$allow.Add_Click({if($list.SelectedIndex -ge 0){[void](Invoke-RouterCli @('provider','set-model-denied',$Id,$models[$list.SelectedIndex],'--allow'));$dialog.Close()}});[void]$buttons.Controls.Add($allow);$deny=New-Object System.Windows.Forms.Button;$deny.Text='拒绝模型';$deny.Font=$buttonFont;$deny.Width=100;$deny.Add_Click({if($list.SelectedIndex -ge 0){[void](Invoke-RouterCli @('provider','set-model-denied',$Id,$models[$list.SelectedIndex]));$dialog.Close()}});[void]$buttons.Controls.Add($deny);$cooldown=New-Object System.Windows.Forms.Button;$cooldown.Text='清除冷却';$cooldown.Font=$buttonFont;$cooldown.Width=100;$cooldown.Add_Click({if($list.SelectedIndex -ge 0){[void](Invoke-RouterCli @('provider','clear-cooldown',$Id,$models[$list.SelectedIndex]));$dialog.Close()}});[void]$buttons.Controls.Add($cooldown);$clear=New-Object System.Windows.Forms.Button;$clear.Text='恢复自动选择';$clear.Font=$buttonFont;$clear.Width=150;$clear.Add_Click({$data=Get-ProviderData;$data.providers.($Id).psobject.Properties.Remove('preferred_runtime_model');Write-JsonAtomic $ProviderConfig $data;$dialog.DialogResult=[System.Windows.Forms.DialogResult]::OK;$dialog.Close()});[void]$buttons.Controls.Add($clear);[void]$dialog.ShowDialog($form);Refresh-Providers
}
function Refresh-Usage {
    $summary = Get-UsageSummary; $codex = Get-CodexStatus
    if ($codex.provider -eq 'XiaoyuRouter') { $warning = '当前推理优先通过 XiaoyuRouter；这不是官方额度结论。' } elseif ($summary.openai -ge 5) { $warning = '当前可能快速消耗 Codex 额度；可按需切换到 XiaoyuRouter。' } else { $warning = '当前 Provider 可能消耗官方 Codex 推理额度。' }
    $usageText.Text = ("官方剩余额度：请在官方用量面板查看。本控制台不会伪造额度。`r`n`r`n本地统计，不是官方额度：`r`n今日任务：{0}`r`n本周任务：{1}`r`nOpenAI Provider：{2}`r`nXiaoyuRouter：{3}`r`nLightboat：{4}`r`nLocal：{5}`r`n失败/超时：{6}`r`n`r`n{7}`r`n`r`nOpenAI 轻量测试档：gpt-5.6-luna，低推理；仅用于小型 smoke，不应用于复杂或高风险任务。`r`n`r`n建议：短小低风险任务使用 XiaoyuRouter/Local；中等编码使用 XiaoyuRouter API；复杂或高风险任务使用更强 OpenAI Codex 或 Strong API + review。" -f $summary.today,$summary.week,$summary.openai,$summary.xiaoyu,$summary.lightboat,$summary.local,$summary.failed,$warning)
}
function Refresh-Home { $router = Get-RouterStatus; $codex = Get-CodexStatus; $statusBox.Text = ("Router：{0}`r`n监听地址：{1}`r`n仅本机：是`r`n网络模式：{2}`r`nCodex Provider：{3}`r`nCodex 模型：{4}`r`nXiaoyuRouter 已安装：{5}" -f $(if ($router.running) { '运行中' } else { '已停止' }),$router.address,$router.mode,$codex.provider,$codex.model,$(if($codex.xiaoyu){'是'}else{'否'})); $diagnosticsText.Text = Redact-Text ((Get-ProviderRows | Format-Table -AutoSize | Out-String) + "`r`n" + $statusBox.Text); Refresh-Providers; Refresh-Usage }
function Add-HomeButton([string]$Caption,[scriptblock]$Action,[ValidateSet('Router','Switch')][string]$Area = 'Router') { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 170; $button.Height = 42; $button.Font = $buttonFont; $button.Margin = New-Object System.Windows.Forms.Padding(5); $button.Add_Click($Action); if($Area -eq 'Router'){[void]$homeButtons.Controls.Add($button)}else{[void]$switchButtons.Controls.Add($button)} }
function Add-ProviderButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 135; $button.Height = 36; $button.Font = $buttonFont; $button.Add_Click($Action); [void]$providerButtons.Controls.Add($button) }
Add-HomeButton '启动 Router' { Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton '停止 Router' { Stop-Router; Refresh-Home }
Add-HomeButton '重启 Router' { Stop-Router; Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton '查看模型' { [System.Windows.Forms.MessageBox]::Show(((Get-RouterStatus).models | ConvertTo-Json -Depth 5),'Router 模型') }
Add-HomeButton 'Router 直连测试' { $smoke = Invoke-RouterDirectSmoke; Add-UsageRecord @{ active_provider = (Get-CodexStatus).provider; active_model = (Get-CodexStatus).model; router_virtual_model = 'xiaoyu-lightboat'; task_mode = 'safe_smoke'; duration_seconds = $smoke.seconds; success = $smoke.success; error_code = $smoke.error_code; estimated_route = 'lightboat'; remote_provider_used = 'YES'; local_provider_used = 'NO' }; [System.Windows.Forms.MessageBox]::Show(("状态：{0}`r`n耗时秒数：{1}`r`n响应内容不会被记录。" -f $smoke.status,$smoke.seconds),'Router 直连测试'); Refresh-Usage }
Add-HomeButton '生成交接文档' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'make-codex-handoff.ps1') 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'交接文档') } 'Switch'
Add-HomeButton '使用小羽 Router' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'switch-codex-with-handoff.ps1') -ToXiaoyu 2>&1; Add-UsageRecord @{ active_provider='XiaoyuRouter'; active_model='xiaoyu-auto'; router_virtual_model='xiaoyu-auto'; task_mode='switch'; success=($LASTEXITCODE -eq 0); estimated_route='router_auto'; remote_provider_used='NO'; local_provider_used='NO' }; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'小羽 Router 切换'); Refresh-Home } 'Switch'
Add-HomeButton '使用 OpenAI Luna（低）' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'switch-codex-with-handoff.ps1') -ToOpenAI -ModelProfile light 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'OpenAI 切换'); Refresh-Home } 'Switch'
Add-HomeButton '查看最近交接' { $handoff = Join-Path $ProjectRoot '.codex-ai-router\handoff.md'; if (Test-Path -LiteralPath $handoff) { Start-Process notepad.exe -ArgumentList ('"' + $handoff + '"') } else { [System.Windows.Forms.MessageBox]::Show('尚未生成交接文档。','交接文档') } } 'Switch'
Add-HomeButton '只读检查' { $router = Get-RouterStatus; $codex = Get-CodexStatus; [System.Windows.Forms.MessageBox]::Show(("Router 运行：{0}`r`n当前 Provider：{1}`r`n当前模型：{2}" -f $router.running,$codex.provider,$codex.model),'只读检查') } 'Switch'
Add-ProviderButton '新增供应商' { [System.Windows.Forms.MessageBox]::Show('标准 Bearer API Key 供应商通常不需要自定义 Header。Groq（https://api.groq.com/openai/v1）使用官方 SDK，默认不配置自定义 Header。','新增供应商提示'); Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider.ps1') + '"') }
Add-ProviderButton '轮换 Header' { $id = Require-SelectedProvider; if($id){ Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider-header.ps1') + '"'); Add-ProviderLog ('打开 Header 配置：' + $id) } }
Add-ProviderButton '刷新列表' { Refresh-Providers }
Add-ProviderButton '启用/禁用' { $id = Require-SelectedProvider; if ($id) { Update-ProviderEnabled $id; Add-ProviderLog ('已切换启用状态：' + $id); Refresh-Providers } }
Add-ProviderButton '删除' { $id = Require-SelectedProvider; if ($id -and [System.Windows.Forms.MessageBox]::Show("删除 $id 的元数据？不会删除环境变量中的密钥。",'确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes -and [System.Windows.Forms.MessageBox]::Show('请再次确认删除供应商元数据。','确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes) { [void](Invoke-RouterCli @('provider','remove',$id)); Add-ProviderLog ('已删除供应商元数据：' + $id); Refresh-Providers } }
Add-ProviderButton '刷新模型' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','refresh-models',$id); Add-ProviderLog ('模型刷新：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'模型刷新'); Refresh-Providers } }
Add-ProviderButton '运行探测' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','probe-runtime',$id); Add-ProviderLog ('运行探测：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'运行探测'); Refresh-Providers } }
Add-ProviderButton '选择模型' { $id = Require-SelectedProvider; if ($id) { Open-ModelPicker $id; Add-ProviderLog ('模型选择：' + $id) } }
Add-ProviderButton '转换为 Groq SDK' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','migrate-groq',$id); Add-ProviderLog ('Groq SDK 转换：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'Groq SDK 转换'); Refresh-Providers } }
Add-ProviderButton '详情' { $id = Require-SelectedProvider; if ($id) { $provider = (Get-ProviderData).providers.($id); $registry=$provider.model_registry; $headerNames = if ($provider.headers) { $provider.headers.psobject.Properties.Name -join ', ' } else { 'NONE' }; $keyConfigured = if ($provider.api_key_env -and [Environment]::GetEnvironmentVariable($provider.api_key_env,'User')) { 'YES' } else { 'NO' }; $discovered=Format-ModelList $(if($registry){$registry.DISCOVERED_MODELS}else{$provider.last_discovery_models}); $usable=Format-ModelList $(if($registry){$registry.USABLE_MODELS}else{$provider.last_discovery_models}); $allowed=Format-ModelList $(if($registry){$registry.ALLOWED_MODELS}else{$provider.allowed_model_seeds}); $denied=Format-ModelList $(if($registry){$registry.DENIED_MODELS}else{@()}); $responsive=Format-ModelList $(if($registry){$registry.RUNTIME_RESPONSIVE_MODELS}else{@()}); $cooldown=Format-ModelList $(if($registry){$registry.TIMEOUT_COOLDOWN_MODELS}else{@()}); $runtime = @((Get-ProviderRows)|Where-Object{$_.provider_id -eq $id}|Select-Object -First 1); $runtimeStatus = if($runtime.Count){$runtime[0].last_runtime_status}else{'UNKNOWN'}; $selected=if($provider.preferred_runtime_model){$provider.preferred_runtime_model}elseif($registry -and $registry.CURRENT_RUNTIME_MODEL){$registry.CURRENT_RUNTIME_MODEL}elseif($runtime.Count){$runtime[0].selected_runtime_model}else{'NONE'}; $selectionMode=if($provider.preferred_runtime_model){'手动选择'}else{'自动选择'}; [System.Windows.Forms.MessageBox]::Show(("Provider ID：{0}`r`n显示名：{1}`r`nBase URL：{2}`r`nWire API：{3}`r`n启用：{4}`r`n发现认证：{5}`r`n推理认证：{6}`r`nAPI Key 已配置：{7}`r`nAPI Key 环境变量：{8}`r`nHeader 名称：{9}`r`n发现模型：{10}`r`n可用模型：{11}`r`n允许模型：{12}`r`n拒绝模型：{13}`r`n运行可用模型：{14}`r`n超时冷却模型：{15}`r`n当前运行模型：{16}`r`n选择方式：{17}`r`n最近发现状态：{18}`r`n最近运行状态：{19}" -f $id,$provider.display_name,$provider.base_url,$provider.wire_api,$provider.enabled,$provider.model_discovery_auth_style,$provider.inference_auth_style,$keyConfigured,$provider.api_key_env,$headerNames,$discovered,$usable,$allowed,$denied,$responsive,$cooldown,$selected,$selectionMode,$provider.last_discovery_status,$runtimeStatus),'供应商详情'); Add-ProviderLog ('查看详情：' + $id) } }
function Add-UsageButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 180; $button.Height = 38; $button.Font = $buttonFont; $button.Add_Click($Action); [void]$usageButtons.Controls.Add($button) }
Add-UsageButton '打开 Codex 用量' { Start-Process 'https://chatgpt.com/#settings'; [System.Windows.Forms.MessageBox]::Show('请在 Codex 设置 → 用量中查看官方额度信息。','官方用量') }
Add-UsageButton '切换省额度模式' { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-xiaoyu-codex.ps1'); Refresh-Home }
Add-UsageButton '刷新本地统计' { Refresh-Usage }
if ($SelfTest) {
    Refresh-Providers
    Write-Output 'CONTROL_UI_INITIALIZATION=PASS'
    Write-Output ('PROVIDER_TABLE_ROWS=' + $grid.Rows.Count)
    Write-Output ('PROVIDER_TABLE_COLUMNS=' + $grid.Columns.Count)
    Write-Output ('PROVIDER_TAB_STATUS=' + (Redact-Text $providerStatus.Text))
    exit 0
}
$form.Add_Shown({ Refresh-Home })
[void]$form.ShowDialog()
