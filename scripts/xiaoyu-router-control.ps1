[CmdletBinding()]
param(
    [switch]$NoShow,
    [switch]$SelfTest,
    [ValidateRange(0.8, 2.0)]
    [double]$FontScale = 1.25,
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
        if ($state) { $passing = @($state.psobject.Properties | Where-Object { $_.Value.status -eq 'PASS' } | Sort-Object { $_.Value.elapsed_seconds }); if ($passing.Count -gt 0) { $selected = $passing[0].Name }; $denied = @($provider.denied_model_ids); if($provider.preferred_runtime_model -and $provider.preferred_runtime_model -notin $denied -and @($passing.Name) -contains $provider.preferred_runtime_model){$selected=$provider.preferred_runtime_model}; if($selected -and $selected -in $denied){$selected=$null}; $latest = @($state.psobject.Properties | Sort-Object { $_.Value.checked_at } -Descending | Select-Object -First 1); if ($latest.Count -gt 0) { $last = [string]$latest[0].Value.status } }
        $registry = $provider.model_registry; $discovered = if($registry -and $registry.DISCOVERED_MODELS){@($registry.DISCOVERED_MODELS)}elseif($provider.last_discovery_models){@($provider.last_discovery_models)}else{@($provider.models)}; $seeds=@($provider.allowed_model_seeds); $usable = if($registry -and $null -ne $registry.USABLE_MODELS){@($registry.USABLE_MODELS)}elseif($seeds.Count -gt 0){@($discovered | Where-Object { $_ -in $seeds })}else{$discovered}
        $rows += [pscustomobject]@{ provider_id = $property.Name; display_name = if ($provider.display_name) { $provider.display_name } else { $property.Name }; provider_type = $provider.type; base_url = $provider.base_url; wire_api = $provider.wire_api; enabled = if ($provider.enabled -eq $false) { 'NO' } else { 'YES' }; discovered_model_count = $discovered.Count; usable_model_count = $usable.Count; last_runtime_status = $last; selected_runtime_model = $selected }
    }
    return $rows
}
function Get-CodexStatus {
    $provider = 'DEFAULT'; $model = 'DEFAULT'; $reasoning = 'DEFAULT'; $xiaoyu = $false
    if (Test-Path -LiteralPath $CodexConfig) { $lines = Get-Content -LiteralPath $CodexConfig -Encoding UTF8; foreach ($line in $lines) { if ($line -match '^\s*\[') { break }; if ($line -match '^\s*model_provider\s*=\s*"([^"]*)"') { $provider = $Matches[1] }; if ($line -match '^\s*model\s*=\s*"([^"]*)"') { $model = $Matches[1] }; if ($line -match '^\s*(?:model_reasoning_effort|reasoning_effort)\s*=\s*"([^"]*)"') { $reasoning = $Matches[1] } }; $xiaoyu = ([string]::Join("`n", $lines) -match '\[model_providers\.XiaoyuRouter\]') }
    return [pscustomobject]@{ provider = $provider; model = $model; reasoning = $reasoning; xiaoyu = $xiaoyu }
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
$homeLayout = New-Object System.Windows.Forms.TableLayoutPanel; $homeLayout.Dock = 'Fill'; $homeLayout.Padding = New-Object System.Windows.Forms.Padding(12); $homeLayout.RowCount = 4; $homeLayout.ColumnCount = 1; [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,175))); $homeTab.Controls.Add($homeLayout)
$routerGroup = New-Object System.Windows.Forms.GroupBox; $routerGroup.Text = 'Router 服务'; $routerGroup.Dock = 'Fill'; $routerGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($routerGroup,0,0)
$homeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $homeButtons.Dock = 'Fill'; $homeButtons.AutoSize = $true; $routerGroup.Controls.Add($homeButtons)
$switchGroup = New-Object System.Windows.Forms.GroupBox; $switchGroup.Text = 'Codex 切换与交接'; $switchGroup.Dock = 'Fill'; $switchGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($switchGroup,0,1)
$switchButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $switchButtons.Dock = 'Fill'; $switchButtons.AutoSize = $true; $switchGroup.Controls.Add($switchButtons)
$statusGroup = New-Object System.Windows.Forms.GroupBox; $statusGroup.Text = '当前状态'; $statusGroup.Dock = 'Fill'; $statusGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($statusGroup,0,2)
$statusBox = New-Object System.Windows.Forms.TextBox; $statusBox.Multiline = $true; $statusBox.ReadOnly = $true; $statusBox.Dock = 'Fill'; $statusBox.ScrollBars = 'Vertical'; $statusBox.Font = $uiFont; $statusGroup.Controls.Add($statusBox)
$directLocalGroup = New-Object System.Windows.Forms.GroupBox; $directLocalGroup.Text = '直接本地模型（推荐）'; $directLocalGroup.Dock = 'Fill'; $directLocalGroup.Padding = New-Object System.Windows.Forms.Padding(8); $homeLayout.Controls.Add($directLocalGroup,0,3)
$directLocalStatus = New-Object System.Windows.Forms.TextBox; $directLocalStatus.Multiline = $true; $directLocalStatus.ReadOnly = $true; $directLocalStatus.Dock = 'Fill'; $directLocalStatus.Font = $uiFont; $directLocalStatus.ScrollBars = 'Vertical'; $directLocalGroup.Controls.Add($directLocalStatus)
$directLocalButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $directLocalButtons.Dock = 'Bottom'; $directLocalButtons.Height = 42; $directLocalButtons.Font = $buttonFont; $directLocalGroup.Controls.Add($directLocalButtons)
$providerLayout = New-Object System.Windows.Forms.TableLayoutPanel; $providerLayout.Dock = 'Fill'; $providerLayout.Padding = New-Object System.Windows.Forms.Padding(12); $providerLayout.RowCount = 4; $providerLayout.ColumnCount = 1; [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); [void]$providerLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,130))); $providersTab.Controls.Add($providerLayout)
$providerToolbar = New-Object System.Windows.Forms.TableLayoutPanel; $providerToolbar.Dock = 'Fill'; $providerToolbar.AutoSize = $true; $providerToolbar.ColumnCount = 1; $providerLayout.Controls.Add($providerToolbar,0,0)
$providerOperationsGroup = New-Object System.Windows.Forms.GroupBox; $providerOperationsGroup.Text = '供应商操作'; $providerOperationsGroup.Dock = 'Fill'; $providerOperationsGroup.Padding = New-Object System.Windows.Forms.Padding(6); [void]$providerToolbar.Controls.Add($providerOperationsGroup)
$providerButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $providerButtons.Dock = 'Fill'; $providerButtons.AutoSize = $true; $providerOperationsGroup.Controls.Add($providerButtons)
$providerModelGroup = New-Object System.Windows.Forms.GroupBox; $providerModelGroup.Text = '模型操作'; $providerModelGroup.Dock = 'Fill'; $providerModelGroup.Padding = New-Object System.Windows.Forms.Padding(6); [void]$providerToolbar.Controls.Add($providerModelGroup)
$providerModelButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $providerModelButtons.Dock = 'Fill'; $providerModelButtons.AutoSize = $true; $providerModelGroup.Controls.Add($providerModelButtons)
$providerMigrationGroup = New-Object System.Windows.Forms.GroupBox; $providerMigrationGroup.Text = '迁移'; $providerMigrationGroup.Dock = 'Fill'; $providerMigrationGroup.Padding = New-Object System.Windows.Forms.Padding(6); [void]$providerToolbar.Controls.Add($providerMigrationGroup)
$providerMigrationButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $providerMigrationButtons.Dock = 'Fill'; $providerMigrationButtons.AutoSize = $true; $providerMigrationGroup.Controls.Add($providerMigrationButtons)
$providerStatus = New-Object System.Windows.Forms.Label; $providerStatus.Dock = 'Fill'; $providerStatus.AutoSize = $true; $providerStatus.Padding = New-Object System.Windows.Forms.Padding(4); $providerStatus.Font = $uiFont; $providerLayout.Controls.Add($providerStatus,0,1)
$grid = New-Object System.Windows.Forms.DataGridView; $grid.Dock = 'Fill'; $grid.ReadOnly = $true; $grid.Font = $uiFont; $grid.ColumnHeadersDefaultCellStyle.Font = $buttonFont; $grid.AutoGenerateColumns = $true; $grid.AutoSizeColumnsMode = 'Fill'; $grid.SelectionMode = 'FullRowSelect'; $grid.MultiSelect = $false; $grid.AllowUserToAddRows = $false; $grid.AllowUserToDeleteRows = $false; $providerLayout.Controls.Add($grid,0,2)
$providerLogGroup = New-Object System.Windows.Forms.GroupBox; $providerLogGroup.Text = '最近操作日志（已脱敏）'; $providerLogGroup.Dock = 'Fill'; $providerLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $providerLayout.Controls.Add($providerLogGroup,0,3)
$providerLog = New-Object System.Windows.Forms.TextBox; $providerLog.Multiline = $true; $providerLog.ReadOnly = $true; $providerLog.ScrollBars = 'Vertical'; $providerLog.Font = $uiFont; $providerLog.Dock = 'Fill'; $providerLogGroup.Controls.Add($providerLog)
$usageText = New-Object System.Windows.Forms.TextBox; $usageText.Multiline = $true; $usageText.ReadOnly = $true; $usageText.Font = $uiFont; $usageText.Dock = 'Fill'; $usageTab.Controls.Add($usageText)
$usageButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $usageButtons.Dock = 'Top'; $usageButtons.Height = 42; $usageTab.Controls.Add($usageButtons)
$diagnosticsText = New-Object System.Windows.Forms.TextBox; $diagnosticsText.Multiline = $true; $diagnosticsText.ReadOnly = $true; $diagnosticsText.Font = $uiFont; $diagnosticsText.Dock = 'Fill'; $diagnosticsTab.Controls.Add($diagnosticsText)
function Add-ProviderLog([string]$Message) { $line = ('[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), (Redact-Text $Message)); $providerLog.AppendText($line + "`r`n") }
function Invoke-LocalCli([string[]]$Arguments) { try { return (Redact-Text (& xiaoyu-router local @Arguments 2>&1 | Out-String)) } catch { return (Redact-Text $_.Exception.Message) } }
function Refresh-DirectLocalCard {
    $statusRaw = Invoke-LocalCli @('status'); $modelsRaw = Invoke-LocalCli @('models'); $status = $null; $models = $null
    try { $status = $statusRaw | ConvertFrom-Json } catch {}; try { $models = $modelsRaw | ConvertFrom-Json } catch {}
    if($status){$selected = if($status.selected_model){$status.selected_model.model_id}else{'未选择'}; $path = if($status.llama_server_path){$status.llama_server_path}else{'未发现'}; $directLocalStatus.Text = ("后端：llama.cpp direct`r`nllama-server：{0}`r`n本地模型：{1}`r`n服务状态：{2}`r`n端点：{3}`r`n模型数：{4}`r`nLM Studio：仅作可选 fallback" -f $path,$selected,$status.server_running,$status.endpoint,$status.model_count)}else{$directLocalStatus.Text = ('直接本地状态不可用：' + $statusRaw)}
}
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
    $list=New-Object System.Windows.Forms.ListBox;$list.Dock='Fill';$list.Font=$uiFont;$list.SelectionMode=[System.Windows.Forms.SelectionMode]::MultiExtended;foreach($model in $models){$item=$runtime.($model);$status=if($item){$item.status}else{'UNKNOWN'};$latency=if($item){$item.elapsed_seconds}else{'-'};$source=if($sources.($model)){$sources.($model)}else{'UNKNOWN'};$manual=if($provider.preferred_runtime_model -eq $model){'（手动首选）'}else{''};[void]$list.Items.Add(('{0} | {1} | {2}s | {3} {4}' -f $model,$status,$latency,$source,$manual));if($provider.preferred_runtime_model -eq $model){$list.SelectedIndex=$list.Items.Count-1}};$layout.Controls.Add($list,0,0)
    $buttons=New-Object System.Windows.Forms.FlowLayoutPanel;$buttons.Dock='Fill';$layout.Controls.Add($buttons,0,1);$selected={@($list.SelectedIndices|ForEach-Object{$models[$_]})};$save=New-Object System.Windows.Forms.Button;$save.Text='设为当前/首选模型';$save.Font=$buttonFont;$save.Width=190;$save.Add_Click({$chosen=& $selected;if($chosen.Count -ne 1){[System.Windows.Forms.MessageBox]::Show('请选择一个模型。','模型选择');return};Set-PreferredRuntimeModel $Id $chosen[0];$dialog.Close()});[void]$buttons.Controls.Add($save);foreach($spec in @(@('批量允许','allow'),@('批量禁用','deny'),@('批量清除禁用','clear_deny'),@('仅使用此模型','only'))){$button=New-Object System.Windows.Forms.Button;$button.Text=$spec[0];$button.Font=$buttonFont;$button.Width=120;$action=[string]$spec[1];$handler={$chosen=& $selected;if($chosen.Count -eq 0){return};if($action -in @('deny','only') -and [System.Windows.Forms.MessageBox]::Show('确认修改模型策略？','确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -ne [System.Windows.Forms.DialogResult]::Yes){return};[void](Invoke-RouterCli (@('provider','batch-model-policy',$Id,$action)+$chosen));$dialog.Close()}.GetNewClosure();$button.Add_Click($handler);[void]$buttons.Controls.Add($button)};$clear=New-Object System.Windows.Forms.Button;$clear.Text='批量清除冷却';$clear.Font=$buttonFont;$clear.Width=120;$clear.Add_Click({foreach($model in (& $selected)){[void](Invoke-RouterCli @('provider','clear-cooldown',$Id,$model))};$dialog.Close()});[void]$buttons.Controls.Add($clear);[void]$dialog.ShowDialog($form);Refresh-Providers
}
function Get-ModelTableData([string]$Id) {
    $provider = (Get-ProviderData).providers.($Id); if(-not $provider){throw 'Provider was not found.'}
    $registry = if($provider.model_registry){$provider.model_registry}else{[pscustomobject]@{}}
    $models = if($registry.DISCOVERED_MODELS){@($registry.DISCOVERED_MODELS)}elseif($provider.last_discovery_models){@($provider.last_discovery_models)}else{@($provider.models)}
    $allowed = @($registry.ALLOWED_MODELS); $denied = @($registry.DENIED_MODELS) + @($provider.denied_model_ids); $runtime = (Read-JsonFile $RuntimeConfig ([pscustomobject]@{providers=[pscustomobject]@{}})).providers.($Id)
    $current = [string]$registry.CURRENT_RUNTIME_MODEL; $preferred = [string]$provider.preferred_runtime_model; $table = New-Object System.Data.DataTable
    foreach($name in @('勾选','model id','发现状态','可用状态','允许/拒绝','priority','runtime status','latency','cooldown','当前模型','首选模型','来源')){[void]$table.Columns.Add($name,[string])}
    $table.Columns['勾选'].DataType = [bool]
    foreach($model in $models){
        $state=$runtime.($model)
        $runtimeStatus=if($state){[string]$state.status}else{'UNKNOWN'}
        $latency=if($state){[string]$state.elapsed_seconds}else{'-'}
        $isAllowed=if($denied -contains $model){'拒绝'}elseif($allowed.Count -eq 0 -or $allowed -contains $model){'允许'}else{'拒绝'}
        $availability=if($isAllowed -eq '允许'){'可用'}else{'不可用'}
        $cooldown=if($state -and $state.status -eq 'TIMEOUT'){'是'}else{'否'}
        $source=if($registry.SOURCES -and $registry.SOURCES.($model)){$registry.SOURCES.($model)}else{'UNKNOWN'}
        $priority=if($provider.model_priorities -and $provider.model_priorities.($model)){$provider.model_priorities.($model)}else{'100'}
        $isCurrent=if($current -eq $model){'是'}else{'否'}
        $isPreferred=if($preferred -eq $model){'是'}else{'否'}
        [void]$table.Rows.Add($false,$model,[string]$provider.last_discovery_status,$availability,$isAllowed,$priority,$runtimeStatus,$latency,$cooldown,$isCurrent,$isPreferred,$source)
    }
    return ,$table
}
function Get-CheckedModels([System.Windows.Forms.DataGridView]$ModelGrid) { @($ModelGrid.Rows | Where-Object {[bool]$_.Cells['勾选'].Value} | ForEach-Object {[string]$_.Cells['model id'].Value}) }
function Confirm-DangerousModelAction([string]$Action) { if($Action -notin @('deny','only')){return $true}; if([System.Windows.Forms.MessageBox]::Show('这是会改变模型策略的操作，是否继续？','第一次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -ne [System.Windows.Forms.DialogResult]::Yes){return $false}; return [System.Windows.Forms.MessageBox]::Show(('请再次确认操作：' + $Action),'第二次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes }
function Invoke-ModelPolicyAction([string]$Id,[string]$Action,[string[]]$Models,[int]$Priority = 100) { if($Models.Count -eq 0 -or -not (Confirm-DangerousModelAction $Action)){return}; $args=@('provider','batch-model-policy',$Id,$Action)+$Models; if($Action -eq 'priority'){$args += @('--priority',[string]$Priority)}; $output=Invoke-RouterCli $args; Add-ProviderLog ('模型策略：' + $Action + '；' + $output.Trim()) }
function Set-ModelPriority([string]$Id,[string[]]$Models) { Add-Type -AssemblyName Microsoft.VisualBasic; $value=[Microsoft.VisualBasic.Interaction]::InputBox('请输入 priority 整数。','设置 priority','100'); $number=0; if([int]::TryParse($value,[ref]$number)){Invoke-ModelPolicyAction $Id 'priority' $Models $number} }
function New-ModelDialogGrid([System.Data.DataTable]$Table) { $modelGrid=New-Object System.Windows.Forms.DataGridView; $modelGrid.Dock='Fill';$modelGrid.Font=$uiFont;$modelGrid.ColumnHeadersDefaultCellStyle.Font=$buttonFont;$modelGrid.AutoGenerateColumns=$true;$modelGrid.AutoSizeColumnsMode='Fill';$modelGrid.AllowUserToAddRows=$false;$modelGrid.AllowUserToDeleteRows=$false;$modelGrid.SelectionMode='FullRowSelect';$modelGrid.MultiSelect=$true;$modelGrid.DataSource=$Table;$modelGrid.ReadOnly=$false;foreach($column in $modelGrid.Columns){$column.ReadOnly=($column.Name -ne '勾选')};return $modelGrid }
function Open-ModelPicker([string]$Id) {
    $table=Get-ModelTableData $Id; if($table.Rows.Count -eq 0){[System.Windows.Forms.MessageBox]::Show('暂无可选择模型。请先刷新模型并运行探测。','模型选择');return};$dialog=New-Object System.Windows.Forms.Form;$dialog.Text=('模型选择：' + $Id);$dialog.Size=New-Object System.Drawing.Size(1180,620);$dialog.StartPosition='CenterParent';$dialog.Font=$uiFont;$layout=New-Object System.Windows.Forms.TableLayoutPanel;$layout.Dock='Fill';$layout.Padding=New-Object System.Windows.Forms.Padding(10);$layout.RowCount=2;$layout.ColumnCount=1;$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)));$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,72)));$dialog.Controls.Add($layout);$modelGrid=New-ModelDialogGrid $table;$layout.Controls.Add($modelGrid,0,0);$buttons=New-Object System.Windows.Forms.FlowLayoutPanel;$buttons.Dock='Fill';$buttons.Font=$buttonFont;$layout.Controls.Add($buttons,0,1)
    function Add-ModelDialogButton([string]$Text,[scriptblock]$Action,[int]$Width=140){$button=New-Object System.Windows.Forms.Button;$button.Text=$Text;$button.Width=$Width;$button.Height=38;$button.Font=$buttonFont;$button.Add_Click($Action);[void]$buttons.Controls.Add($button)}
    Add-ModelDialogButton '设为当前模型' { $chosen=Get-CheckedModels $modelGrid;if($chosen.Count -eq 1){[void](Invoke-RouterCli @('provider','set-runtime-model',$Id,$chosen[0]));$dialog.Close()} }
    Add-ModelDialogButton '设为首选模型' { $chosen=Get-CheckedModels $modelGrid;if($chosen.Count -eq 1){[void](Invoke-RouterCli @('provider','set-runtime-model',$Id,$chosen[0]));$dialog.Close()} }
    Add-ModelDialogButton '仅使用此模型' { $chosen=Get-CheckedModels $modelGrid;if($chosen.Count -eq 1){Invoke-ModelPolicyAction $Id 'only' $chosen;$dialog.Close()} }
    Add-ModelDialogButton '允许选中' {Invoke-ModelPolicyAction $Id 'allow' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-ModelDialogButton '禁用选中' {Invoke-ModelPolicyAction $Id 'deny' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-ModelDialogButton '清除选中禁用' {Invoke-ModelPolicyAction $Id 'clear_deny' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-ModelDialogButton '设置 priority' {Set-ModelPriority $Id (Get-CheckedModels $modelGrid)}
    Add-ModelDialogButton '清除 cooldown' {foreach($model in (Get-CheckedModels $modelGrid)){[void](Invoke-RouterCli @('provider','clear-cooldown',$Id,$model))};$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-ModelDialogButton '刷新' { [void](Invoke-RouterCli @('provider','refresh-models',$Id));$modelGrid.DataSource=Get-ModelTableData $Id }
    Add-ModelDialogButton '保存并关闭' {$dialog.Close()} 150
    [void]$dialog.ShowDialog($form);$dialog.Dispose();Refresh-Providers
}
function Set-ModelGridFilter([System.Windows.Forms.DataGridView]$ModelGrid,[string]$Filter) { $needle=([string]$Filter).Trim(); foreach($row in $ModelGrid.Rows){$model=[string]$row.Cells['model id'].Value;$status=[string]$row.Cells['runtime status'].Value;$row.Visible=([string]::IsNullOrWhiteSpace($needle) -or $model.IndexOf($needle,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or $status.IndexOf($needle,[System.StringComparison]::OrdinalIgnoreCase) -ge 0)} }
function Open-BatchManager([string]$Id) {
    $table=Get-ModelTableData $Id;if($table.Rows.Count -eq 0){[System.Windows.Forms.MessageBox]::Show('暂无可批量管理的模型。','批量管理');return};$dialog=New-Object System.Windows.Forms.Form;$dialog.Text=('批量模型管理：' + $Id);$dialog.Size=New-Object System.Drawing.Size(1080,640);$dialog.StartPosition='CenterParent';$dialog.Font=$uiFont;$layout=New-Object System.Windows.Forms.TableLayoutPanel;$layout.Dock='Fill';$layout.Padding=New-Object System.Windows.Forms.Padding(10);$layout.RowCount=3;$layout.ColumnCount=1;$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,42)));$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)));$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,110)));$dialog.Controls.Add($layout);$filterPanel=New-Object System.Windows.Forms.FlowLayoutPanel;$filterPanel.Dock='Fill';$filterPanel.Font=$uiFont;$filterLabel=New-Object System.Windows.Forms.Label;$filterLabel.Text='筛选模型/状态：';$filterLabel.AutoSize=$true;$filterLabel.Padding=New-Object System.Windows.Forms.Padding(0,7,0,0);$filterPanel.Controls.Add($filterLabel);$filterBox=New-Object System.Windows.Forms.TextBox;$filterBox.Width=260;$filterBox.Height=30;$filterBox.Font=$uiFont;$filterPanel.Controls.Add($filterBox);$layout.Controls.Add($filterPanel,0,0);$modelGrid=New-ModelDialogGrid $table;$layout.Controls.Add($modelGrid,0,1);$filterBox.Add_TextChanged({Set-ModelGridFilter $modelGrid $filterBox.Text}.GetNewClosure());$buttons=New-Object System.Windows.Forms.FlowLayoutPanel;$buttons.Dock='Fill';$buttons.Font=$buttonFont;$layout.Controls.Add($buttons,0,2)
    function Add-BatchButton([string]$Text,[scriptblock]$Action,[int]$Width=135){$button=New-Object System.Windows.Forms.Button;$button.Text=$Text;$button.Width=$Width;$button.Height=36;$button.Font=$buttonFont;$button.Add_Click($Action);[void]$buttons.Controls.Add($button)}
    Add-BatchButton '全选' {foreach($row in $modelGrid.Rows){$row.Cells['勾选'].Value=$true}}
    Add-BatchButton '反选' {foreach($row in $modelGrid.Rows){$row.Cells['勾选'].Value=-not [bool]$row.Cells['勾选'].Value}}
    Add-BatchButton '选择 PASS' {foreach($row in $modelGrid.Rows){$row.Cells['勾选'].Value=($row.Cells['runtime status'].Value -eq 'PASS')}}
    Add-BatchButton '选择 TIMEOUT' {foreach($row in $modelGrid.Rows){$row.Cells['勾选'].Value=($row.Cells['runtime status'].Value -eq 'TIMEOUT')}}
    Add-BatchButton '选择 UNKNOWN' {foreach($row in $modelGrid.Rows){$row.Cells['勾选'].Value=($row.Cells['runtime status'].Value -eq 'UNKNOWN')}}
    Add-BatchButton '批量允许' {Invoke-ModelPolicyAction $Id 'allow' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-BatchButton '批量禁用' {Invoke-ModelPolicyAction $Id 'deny' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-BatchButton '批量清除禁用' {Invoke-ModelPolicyAction $Id 'clear_deny' (Get-CheckedModels $modelGrid);$modelGrid.DataSource=Get-ModelTableData $Id}
    Add-BatchButton '批量 priority' {Set-ModelPriority $Id (Get-CheckedModels $modelGrid)}
    Add-BatchButton '批量清除 cooldown' {foreach($model in (Get-CheckedModels $modelGrid)){[void](Invoke-RouterCli @('provider','clear-cooldown',$Id,$model))}}
    Add-BatchButton '只保留当前模型' {$current=(Get-ProviderData).providers.($Id).model_registry.CURRENT_RUNTIME_MODEL;if($current){Invoke-ModelPolicyAction $Id 'only' @([string]$current);$modelGrid.DataSource=Get-ModelTableData $Id}}
    Add-BatchButton '保存并关闭' {$dialog.Close()} 150
    [void]$dialog.ShowDialog($form);$dialog.Dispose();Refresh-Providers
}
function Test-ModelDialogConstruction([string]$Id) { $table=Get-ModelTableData $Id;$picker=New-ModelDialogGrid $table;$batch=New-ModelDialogGrid $table;$picker.Dispose();$batch.Dispose();return ($table.Columns.Contains('勾选') -and $table.Columns.Contains('runtime status')) }
function Refresh-Usage {
    $summary = Get-UsageSummary; $codex = Get-CodexStatus
    if ($codex.provider -eq 'XiaoyuRouter') { $warning = '当前推理优先通过 XiaoyuRouter；这不是官方额度结论。' } elseif ($summary.openai -ge 5) { $warning = '当前可能快速消耗 Codex 额度；可按需切换到 XiaoyuRouter。' } else { $warning = '当前 Provider 可能消耗官方 Codex 推理额度。' }
    $usageText.Text = ("官方剩余额度：请在官方用量面板查看。本控制台不会伪造额度。`r`n`r`n本地统计，不是官方额度：`r`n今日任务：{0}`r`n本周任务：{1}`r`nOpenAI Provider：{2}`r`nXiaoyuRouter：{3}`r`nLightboat：{4}`r`nLocal：{5}`r`n失败/超时：{6}`r`n`r`n{7}`r`n`r`nOpenAI 轻量测试档：gpt-5.6-luna，低推理；仅用于小型 smoke，不应用于复杂或高风险任务。`r`n`r`n建议：短小低风险任务使用 XiaoyuRouter/Local；中等编码使用 XiaoyuRouter API；复杂或高风险任务使用更强 OpenAI Codex 或 Strong API + review。" -f $summary.today,$summary.week,$summary.openai,$summary.xiaoyu,$summary.lightboat,$summary.local,$summary.failed,$warning)
}
function Refresh-Home { $router = Get-RouterStatus; $codex = Get-CodexStatus; $statusBox.Text = ("Router：{0}`r`n监听地址：{1}`r`n仅本机：是`r`n网络模式：{2}`r`nCodex Provider：{3}`r`nCodex 模型：{4}`r`nCodex 推理强度：{5}`r`nXiaoyuRouter 已安装：{6}`r`n`r`n官方委托提示：Luna+low 适合轻量调度；Terra+medium 适合中等实现；高风险建议 Sol/最强模型 + high/xhigh。委托不会自动热切当前 Codex 模型。" -f $(if ($router.running) { '运行中' } else { '已停止' }),$router.address,$router.mode,$codex.provider,$codex.model,$codex.reasoning,$(if($codex.xiaoyu){'是'}else{'否'})); $diagnosticsText.Text = Redact-Text ((Get-ProviderRows | Format-Table -AutoSize | Out-String) + "`r`n" + $statusBox.Text); Refresh-Providers; Refresh-Usage; Refresh-DirectLocalCard }
function Add-HomeButton([string]$Caption,[scriptblock]$Action,[ValidateSet('Router','Switch')][string]$Area = 'Router') { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 170; $button.Height = 42; $button.Font = $buttonFont; $button.Margin = New-Object System.Windows.Forms.Padding(5); $button.Add_Click($Action); if($Area -eq 'Router'){[void]$homeButtons.Controls.Add($button)}else{[void]$switchButtons.Controls.Add($button)} }
function Add-DirectLocalButton([string]$Caption,[scriptblock]$Action,[int]$Width=150) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=34; $button.Font=$buttonFont; $button.Add_Click($Action); [void]$directLocalButtons.Controls.Add($button) }
function Add-ProviderButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 135; $button.Height = 36; $button.Font = $buttonFont; $button.Add_Click($Action); $target = if($Caption -in @('刷新模型','运行探测','选择模型','批量管理','解释选择')){$providerModelButtons}elseif($Caption -eq '转换为 Groq SDK'){$providerMigrationButtons}else{$providerButtons}; [void]$target.Controls.Add($button) }
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
Add-HomeButton '启用官方委托模式' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-official-delegation-codex.ps1') 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'官方 Codex + 小羽委托') } 'Switch'
Add-HomeButton '生成官方使用说明' { $path = Join-Path $ProjectRoot '.codex-ai-router\official-delegation-instructions.md'; if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('使用说明尚未生成。请先启用官方委托模式。','官方委托')} } 'Switch'
Add-HomeButton '测试委托' { $out=& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'xiaoyu-delegate.ps1') -Task '用一句话说明当前项目用途，不修改文件' -Mode read -Risk simple -MaxSeconds 20 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'委托测试') } 'Switch'
Add-HomeButton '查看最近委托日志' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\delegation-ledger.jsonl'; if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无本地委托记录。','委托日志')} } 'Switch'
Add-DirectLocalButton '选择 llama-server.exe' { $picker=New-Object System.Windows.Forms.OpenFileDialog; $picker.Filter='llama-server|llama-server.exe|所有文件|*.*'; if($picker.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK){[void](Invoke-LocalCli @('configure','--llama-server-path',$picker.FileName));Refresh-DirectLocalCard} }
Add-DirectLocalButton '添加模型目录' { $picker=New-Object System.Windows.Forms.FolderBrowserDialog; if($picker.ShowDialog($form) -eq [System.Windows.Forms.DialogResult]::OK){[void](Invoke-LocalCli @('configure','--model-dir',$picker.SelectedPath));Refresh-DirectLocalCard} }
Add-DirectLocalButton '扫描 LM Studio 模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('models')),'Direct Local GGUF 模型') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '选择本地模型' { Add-Type -AssemblyName Microsoft.VisualBasic; $value=[Microsoft.VisualBasic.Interaction]::InputBox('输入已发现的 GGUF model_id：','选择本地模型',''); if($value){[System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('select',$value)),'本地模型');Refresh-DirectLocalCard} }
Add-DirectLocalButton '启动本地后端' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('start')),'Direct Local 启动');Refresh-DirectLocalCard }
Add-DirectLocalButton '停止本地后端' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('stop')),'Direct Local 停止');Refresh-DirectLocalCard }
Add-DirectLocalButton '测试本地推理' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('smoke','只回复 LOCAL_DIRECT_OK')),'Direct Local Smoke');Refresh-DirectLocalCard }
Add-ProviderButton '新增供应商' { [System.Windows.Forms.MessageBox]::Show('标准 Bearer API Key 供应商通常不需要自定义 Header。Groq（https://api.groq.com/openai/v1）使用官方 SDK，默认不配置自定义 Header。','新增供应商提示'); Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider.ps1') + '"') }
Add-ProviderButton '轮换 Header' { $id = Require-SelectedProvider; if($id){ Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider-header.ps1') + '"'); Add-ProviderLog ('打开 Header 配置：' + $id) } }
Add-ProviderButton '刷新列表' { Refresh-Providers }
Add-ProviderButton '启用/禁用' { $id = Require-SelectedProvider; if ($id) { Update-ProviderEnabled $id; Add-ProviderLog ('已切换启用状态：' + $id); Refresh-Providers } }
Add-ProviderButton '删除' { $id = Require-SelectedProvider; if ($id -and [System.Windows.Forms.MessageBox]::Show("删除 $id 的元数据？不会删除环境变量中的密钥。",'确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes -and [System.Windows.Forms.MessageBox]::Show('请再次确认删除供应商元数据。','确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes) { [void](Invoke-RouterCli @('provider','remove',$id)); Add-ProviderLog ('已删除供应商元数据：' + $id); Refresh-Providers } }
Add-ProviderButton '刷新模型' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','refresh-models',$id); Add-ProviderLog ('模型刷新：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'模型刷新'); Refresh-Providers } }
Add-ProviderButton '运行探测' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','probe-runtime',$id); Add-ProviderLog ('运行探测：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'运行探测'); Refresh-Providers } }
Add-ProviderButton '选择模型' { $id = Require-SelectedProvider; if ($id) { Open-ModelPicker $id; Add-ProviderLog ('模型选择：' + $id) } }
Add-ProviderButton '批量管理' { $id=Require-SelectedProvider;if($id){Open-BatchManager $id;Add-ProviderLog ('批量管理：'+$id)}}
Add-ProviderButton '解释选择' { $id=Require-SelectedProvider;if($id){$out=Invoke-RouterCli @('provider','explain-selection',('xiaoyu-api-' + $id));[System.Windows.Forms.MessageBox]::Show($out,'选择解释')}}
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
    $dialogSmoke = $false
    try {
        $selfRows = @(Get-ProviderRows)
        if ($selfRows.Count -gt 0) { $dialogSmoke = Test-ModelDialogConstruction $selfRows[0].provider_id }
    } catch {}
    Write-Output ('MODEL_PICKER_UI_CONSTRUCTION=' + $(if($dialogSmoke){'PASS'}else{'SKIPPED_NO_PROVIDER'}))
    Write-Output ('BATCH_UI_CONSTRUCTION=' + $(if($dialogSmoke){'PASS'}else{'SKIPPED_NO_PROVIDER'}))
    Write-Output 'DIRECT_LOCAL_UI_CONSTRUCTION=PASS'
    exit 0
}
$form.Add_Shown({ Refresh-Home })
[void]$form.ShowDialog()
