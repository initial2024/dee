[CmdletBinding()]
param(
    [switch]$NoShow,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProviderConfig = if ($env:XIAOYU_ROUTER_PROVIDER_CONFIG) { $env:XIAOYU_ROUTER_PROVIDER_CONFIG } else { Join-Path $env:USERPROFILE '.codex-ai-router\providers.json' }
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
function Get-ProviderData { return (Read-JsonFile $ProviderConfig ([pscustomobject]@{ providers = [pscustomobject]@{} })) }
function Get-ProviderRows {
    $data = Get-ProviderData; $runtime = Read-JsonFile $RuntimeConfig ([pscustomobject]@{ providers = [pscustomobject]@{} }); $rows = @()
    foreach ($property in @($data.providers.psobject.Properties)) {
        $provider = $property.Value; $state = $runtime.providers.($property.Name); $selected = $null; $last = 'UNKNOWN'
        if ($state) { $passing = @($state.psobject.Properties | Where-Object { $_.Value.status -eq 'PASS' } | Sort-Object { $_.Value.elapsed_seconds }); if ($passing.Count -gt 0) { $selected = $passing[0].Name }; $latest = @($state.psobject.Properties | Sort-Object { $_.Value.checked_at } -Descending | Select-Object -First 1); if ($latest.Count -gt 0) { $last = [string]$latest[0].Value.status } }
        $rows += [pscustomobject]@{ provider_id = $property.Name; display_name = if ($provider.display_name) { $provider.display_name } else { $property.Name }; provider_type = $provider.type; base_url = $provider.base_url; wire_api = $provider.wire_api; enabled = if ($provider.enabled -eq $false) { 'NO' } else { 'YES' }; model_count = @($provider.models).Count; last_runtime_status = $last; selected_runtime_model = $selected }
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

$form = New-Object System.Windows.Forms.Form; $form.Text = 'Xiaoyu Router Control Panel'; $form.Size = New-Object System.Drawing.Size(1120,700); $form.StartPosition = 'CenterScreen'
$tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = 'Fill'; $form.Controls.Add($tabs)
$homeTab = New-Object System.Windows.Forms.TabPage('Home'); $providersTab = New-Object System.Windows.Forms.TabPage('Providers'); $usageTab = New-Object System.Windows.Forms.TabPage('Usage Guard'); $diagnosticsTab = New-Object System.Windows.Forms.TabPage('Diagnostics'); [void]$tabs.TabPages.AddRange(@($homeTab,$providersTab,$usageTab,$diagnosticsTab))
$statusBox = New-Object System.Windows.Forms.TextBox; $statusBox.Multiline = $true; $statusBox.ReadOnly = $true; $statusBox.Dock = 'Top'; $statusBox.Height = 150; $homeTab.Controls.Add($statusBox)
$homeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $homeButtons.Dock = 'Top'; $homeButtons.Top = 155; $homeButtons.Height = 42; $homeTab.Controls.Add($homeButtons)
$grid = New-Object System.Windows.Forms.DataGridView; $grid.Dock = 'Fill'; $grid.ReadOnly = $true; $grid.AutoSizeColumnsMode = 'Fill'; $providersTab.Controls.Add($grid)
$providerButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $providerButtons.Dock = 'Top'; $providerButtons.Height = 42; $providersTab.Controls.Add($providerButtons)
$usageText = New-Object System.Windows.Forms.TextBox; $usageText.Multiline = $true; $usageText.ReadOnly = $true; $usageText.Dock = 'Fill'; $usageTab.Controls.Add($usageText)
$usageButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $usageButtons.Dock = 'Top'; $usageButtons.Height = 42; $usageTab.Controls.Add($usageButtons)
$diagnosticsText = New-Object System.Windows.Forms.TextBox; $diagnosticsText.Multiline = $true; $diagnosticsText.ReadOnly = $true; $diagnosticsText.Dock = 'Fill'; $diagnosticsTab.Controls.Add($diagnosticsText)
function Refresh-Providers { $grid.DataSource = @(Get-ProviderRows) }
function Selected-Provider { if ($grid.CurrentRow) { return [string]$grid.CurrentRow.Cells['provider_id'].Value }; return $null }
function Refresh-Usage {
    $summary = Get-UsageSummary; $codex = Get-CodexStatus
    if ($codex.provider -eq 'XiaoyuRouter') { $warning = 'Inference is routed through XiaoyuRouter first. This is not an official quota conclusion.' } elseif ($summary.openai -ge 5) { $warning = 'Current activity may consume Codex quota quickly. Switch to XiaoyuRouter if appropriate.' } else { $warning = 'Current provider may consume official Codex inference quota.' }
    $usageText.Text = ("Official remaining quota: open the official Usage panel. This console never fabricates a quota balance.`n`nLocal estimate, not official quota:`nToday tasks: {0}`nWeek tasks: {1}`nOpenAI Provider tasks: {2}`nXiaoyuRouter tasks: {3}`nLightboat tasks: {4}`nLocal tasks: {5}`nFailures/timeouts: {6}`n`n{7}`n`nOpenAI light test profile: gpt-5.6-luna with low reasoning, for smoke tests only. Do not use it as the default for complex or high-risk work.`n`nRouting guidance: short low-risk tasks -> XiaoyuRouter/Local; medium coding -> XiaoyuRouter API; complex or high-risk -> stronger OpenAI Codex or strong API plus review." -f $summary.today,$summary.week,$summary.openai,$summary.xiaoyu,$summary.lightboat,$summary.local,$summary.failed,$warning)
}
function Refresh-Home { $router = Get-RouterStatus; $codex = Get-CodexStatus; $statusBox.Text = ("Router: {0}`nListen: {1}`nLocalhost only: YES`nNetwork mode: {2}`nCodex provider: {3}`nCodex model: {4}`nXiaoyuRouter installed: {5}" -f $(if ($router.running) { 'RUNNING' } else { 'STOPPED' }),$router.address,$router.mode,$codex.provider,$codex.model,$codex.xiaoyu); $diagnosticsText.Text = Redact-Text ((Get-ProviderRows | Format-Table -AutoSize | Out-String) + "`n" + $statusBox.Text); Refresh-Providers; Refresh-Usage }
function Add-HomeButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 140; $button.Height = 32; $button.Add_Click($Action); [void]$homeButtons.Controls.Add($button) }
function Add-ProviderButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 120; $button.Height = 30; $button.Add_Click($Action); [void]$providerButtons.Controls.Add($button) }
Add-HomeButton 'Start Router' { Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton 'Stop Router' { Stop-Router; Refresh-Home }
Add-HomeButton 'Restart Router' { Stop-Router; Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton 'Router Models' { [System.Windows.Forms.MessageBox]::Show(((Get-RouterStatus).models | ConvertTo-Json -Depth 5),'Router models') }
Add-HomeButton 'Generate Handoff' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'make-codex-handoff.ps1') 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'Handoff') }
Add-HomeButton 'Xiaoyu + Handoff' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'switch-codex-with-handoff.ps1') -ToXiaoyu 2>&1; Add-UsageRecord @{ active_provider='XiaoyuRouter'; active_model='xiaoyu-auto'; router_virtual_model='xiaoyu-auto'; task_mode='switch'; success=($LASTEXITCODE -eq 0); estimated_route='router_auto'; remote_provider_used='NO'; local_provider_used='NO' }; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'Xiaoyu switch'); Refresh-Home }
Add-HomeButton 'OpenAI + Handoff' { $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'switch-codex-with-handoff.ps1') -ToOpenAI -ModelProfile light 2>&1; [System.Windows.Forms.MessageBox]::Show((Redact-Text ($out | Out-String)),'OpenAI switch'); Refresh-Home }
Add-HomeButton 'View Recent Handoff' { $handoff = Join-Path $ProjectRoot '.codex-ai-router\handoff.md'; if (Test-Path -LiteralPath $handoff) { Start-Process notepad.exe -ArgumentList ('"' + $handoff + '"') } else { [System.Windows.Forms.MessageBox]::Show('No handoff has been generated yet.','Handoff') } }
Add-HomeButton 'Read-only check' { $router = Get-RouterStatus; $codex = Get-CodexStatus; [System.Windows.Forms.MessageBox]::Show(("Router running: {0}`nActive provider: {1}`nActive model: {2}" -f $router.running,$codex.provider,$codex.model),'Read-only check') }
Add-HomeButton 'Router Direct Smoke' { $smoke = Invoke-RouterDirectSmoke; Add-UsageRecord @{ active_provider = (Get-CodexStatus).provider; active_model = (Get-CodexStatus).model; router_virtual_model = 'xiaoyu-lightboat'; task_mode = 'safe_smoke'; duration_seconds = $smoke.seconds; success = $smoke.success; error_code = $smoke.error_code; estimated_route = 'lightboat'; remote_provider_used = 'YES'; local_provider_used = 'NO' }; [System.Windows.Forms.MessageBox]::Show(("Status: {0}`nDuration seconds: {1}`nResponse content is intentionally not logged." -f $smoke.status,$smoke.seconds),'Router direct smoke'); Refresh-Usage }
Add-ProviderButton 'Add Provider' { Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider.ps1') + '"') }
Add-ProviderButton 'Rotate Header' { Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider-header.ps1') + '"') }
Add-ProviderButton 'Refresh' { Refresh-Providers }
Add-ProviderButton 'Enable/Disable' { $id = Selected-Provider; if ($id) { Update-ProviderEnabled $id; Refresh-Providers } }
Add-ProviderButton 'Remove' { $id = Selected-Provider; if ($id -and [System.Windows.Forms.MessageBox]::Show("Remove $id metadata? Environment-variable secrets are not removed.",'Confirm',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes -and [System.Windows.Forms.MessageBox]::Show('Confirm removal again.','Confirm',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes) { [void](Invoke-RouterCli @('provider','remove',$id)); Refresh-Providers } }
Add-ProviderButton 'Refresh Models' { $id = Selected-Provider; if ($id) { [System.Windows.Forms.MessageBox]::Show((Invoke-RouterCli @('provider','refresh-models',$id)),'Model refresh') } }
Add-ProviderButton 'Runtime Probe' { $id = Selected-Provider; if ($id) { [System.Windows.Forms.MessageBox]::Show((Invoke-RouterCli @('provider','probe-runtime',$id)),'Runtime probe') } }
Add-ProviderButton 'Details' { $id = Selected-Provider; if ($id) { $provider = (Get-ProviderData).providers.($id); $headerNames = if ($provider.headers) { $provider.headers.psobject.Properties.Name -join ', ' } else { 'NONE' }; $keyConfigured = if ($provider.api_key_env -and [Environment]::GetEnvironmentVariable($provider.api_key_env,'User')) { 'YES' } else { 'NO' }; [System.Windows.Forms.MessageBox]::Show(("Provider: {0}`nDisplay: {1}`nBase URL: {2}`nWire API: {3}`nEnabled: {4}`nAPI key configured: {5}`nAPI key env: {6}`nHeader names: {7}`nDiscovery auth: {8}`nInference auth: {9}" -f $id,$provider.display_name,$provider.base_url,$provider.wire_api,$provider.enabled,$keyConfigured,$provider.api_key_env,$headerNames,$provider.model_discovery_auth_style,$provider.inference_auth_style),'Provider details') } }
function Add-UsageButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 160; $button.Height = 30; $button.Add_Click($Action); [void]$usageButtons.Controls.Add($button) }
Add-UsageButton 'Open Codex Usage' { Start-Process 'https://chatgpt.com/#settings'; [System.Windows.Forms.MessageBox]::Show('Open Codex Settings then Usage to see official quota information.','Official Usage') }
Add-UsageButton 'Save quota mode' { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-xiaoyu-codex.ps1'); Refresh-Home }
Add-UsageButton 'Refresh local trends' { Refresh-Usage }
if ($SelfTest) { Write-Output 'CONTROL_UI_INITIALIZATION=PASS'; exit 0 }
$form.Add_Shown({ Refresh-Home })
[void]$form.ShowDialog()
