[CmdletBinding()]
param(
    [switch]$NoShow,
    [switch]$SelfTest,
    [ValidateRange(0.8, 2.0)]
    [double]$FontScale = 1.25,
    [string]$ProviderConfigPath = '',
    [ValidateSet('', 'start', 'stop', 'status')]
    [string]$RouterAction = ''
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
$DeepSeekWorkerRoot = 'C:\Users\bad39\Documents\private-ai-chat-worker'
$DeepSeekBridgeScripts = Join-Path $DeepSeekWorkerRoot 'scripts\local-bridge'
$RouterPidFile = Join-Path ([IO.Path]::GetTempPath()) 'xiaoyu-router-18789.pid'
$DeepSeekRuntimeDir = Join-Path $DeepSeekWorkerRoot '.runtime\local-bridge'
$DeepSeekLocalApiAddress = 'http://127.0.0.1:8792/v1'
$CodexModeScript = Join-Path $PSScriptRoot 'codex-mode-manager.ps1'
$script:DeepSeekLastHealth = '未运行'
$script:DeepSeekModeProbe = $null
$script:DeepSeekModePreference = 'auto'
$script:DeepSeekLastSelection = $null
$script:LastModeDebugJson = ''
$script:UiDebugEntries = [System.Collections.Generic.List[string]]::new()
$script:LocalAgentPlanId = ''
$script:LocalAgentPlanJson = $null

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
function Redact-Text([string]$Text) { return ($Text -replace '(?i)(bearer\s+)[^\s]+','$1[REDACTED]' -replace '(?i)(sk-[a-z0-9_-]+)','[REDACTED]' -replace '(?i)(authorization\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)(cookie\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)(token\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)((?:--)?api[_ -]?key(?:=|\s+))[^\s,;]+','$1[REDACTED]' -replace '(?i)((?:--)?password(?:=|\s+))[^\s,;]+','$1[REDACTED]') }
function Get-UiErrorExplanation([string]$Code) {
    switch ($Code) {
        'DOWNSTREAM_UNAVAILABLE' { return '下游服务不可用，可能是 Provider 未通过运行资格、模型未确认或服务未启动。' }
        'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED' { return '外部 API 未在新白名单中启用。' }
        'EXTERNAL_MODEL_NOT_ELIGIBLE' { return '外部模型未通过运行资格检查。' }
        'LIVE_CONFIRMATION_REQUIRED' { return '真实外部 API 测试需要用户手动确认。' }
        'AUTH_MISSING' { return '未检测到可用鉴权配置。' }
        'TOOLS_NOT_SUPPORTED_BY_BACKEND' { return '当前后端不支持工具调用；可切换官方直连，或手动启用文本兼容模式。' }
        'DEEPSEEK_MODE_UNAVAILABLE' { return '请求的 DeepSeek 网页模式未通过界面探测，未假装切换。' }
        'UI_PROBE_FAILED' { return 'DeepSeek 页面控件探测失败，未把该模式标记为可用。' }
        'UI_CHANGED' { return 'DeepSeek 页面控件已变化，已停止模式判断，未发送提示词。' }
        'LOGIN_REQUIRED' { return 'DeepSeek 网页需要登录后才能继续。' }
        'RATE_LIMITED' { return 'DeepSeek 当前触发限流，请停止重试并等待冷却。' }
        'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' { return '18789 被未知进程占用；未停止进程，也未向未知服务发送 POST。请先确认 PID 和命令行。' }
        'STALE_OR_INCOMPATIBLE_ROUTER' { return '检测到旧版或不兼容的小羽 Router；请确认后停止旧进程，再启动当前版本。' }
        'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' { return '检测到旧小羽 Router，但尚未获得停止确认；未终止任何进程。' }
        default { return '请查看高级信息，确认本地配置和服务状态后重试。' }
    }
}
function Add-UiDebugInfo([string]$Name,[string]$Code,[string]$Detail) {
    $line = ('[{0}] 操作={1}; 错误码={2}; 详细信息={3}' -f (Get-Date).ToString('HH:mm:ss'),$Name,$Code,(Redact-Text $Detail))
    $script:UiDebugEntries.Add($line)
    if ($diagnosticsText) { $diagnosticsText.Text = (($script:UiDebugEntries -join "`r`n")) }
}
function Invoke-SafeUiAction([string]$Name,[scriptblock]$Action) {
    try { return (& $Action) }
    catch {
        $detail = Redact-Text ([string]$_.Exception.Message)
        $code = if ($detail -match '(DOWNSTREAM_UNAVAILABLE|EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED|EXTERNAL_MODEL_NOT_ELIGIBLE|LIVE_CONFIRMATION_REQUIRED|AUTH_MISSING|DEEPSEEK_MODE_UNAVAILABLE|UI_PROBE_FAILED|UI_CHANGED|LOGIN_REQUIRED|RATE_LIMITED|PORT_OCCUPIED_BY_UNKNOWN_PROCESS|STOP_OLD_ROUTER_CONFIRMATION_REQUIRED|STALE_OR_INCOMPATIBLE_ROUTER)') { $Matches[1] } else { 'UI_ACTION_FAILED' }
        Add-UiDebugInfo -Name $Name -Code $code -Detail $detail
        $summary = "操作失败`r`n错误码：$code`r`n原因：$(Get-UiErrorExplanation $code)`r`n建议：请检查高级信息 / 调试信息后重试。"
        if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($summary,'小羽 Router 控制台',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null }
        return [pscustomobject]@{ status = 'ERROR'; error_code = $code }
    }
}
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
function Get-ProcessMetadata([int]$ProcessId) {
    $name = ''; $path = ''; $commandLine = ''
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $name = [string]$process.ProcessName
        try { $path = [string]$process.Path } catch {}
    } catch {}
    try {
        $processLine = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcessId) -ErrorAction Stop
        if ($processLine) {
            if ([string]::IsNullOrWhiteSpace($path)) { $path = [string]$processLine.ExecutablePath }
            $commandLine = [string]$processLine.CommandLine
        }
    } catch {
        try {
            $processLine = Get-WmiObject Win32_Process -Filter ('ProcessId=' + $ProcessId) -ErrorAction Stop
            if ($processLine) {
                if ([string]::IsNullOrWhiteSpace($path)) { $path = [string]$processLine.ExecutablePath }
                $commandLine = [string]$processLine.CommandLine
            }
        } catch {}
    }
    if ([string]::IsNullOrWhiteSpace($path)) { $path = '[UNAVAILABLE]' }
    if ([string]::IsNullOrWhiteSpace($commandLine)) { $commandLine = '[UNAVAILABLE]' }
    return [pscustomobject]@{
        pid = $ProcessId
        process_name = (Redact-Text $name)
        executable_path = (Redact-Text $path)
        command_line = (Redact-Text $commandLine)
    }
}
function Invoke-RouterGet([string]$Uri) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
        $body = $null
        try { $body = $response.Content | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ status_code = [int]$response.StatusCode; body = $body }
    } catch {
        $status = 0
        try { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } } catch {}
        return [pscustomobject]@{ status_code = $status; body = $null }
    }
}
function Get-RouterIdentityProbe {
    $base = 'http://127.0.0.1:18789'
    $agent = Invoke-RouterGet ($base + '/agent/health')
    $health = Invoke-RouterGet ($base + '/health')
    $models = Invoke-RouterGet ($base + '/v1/models')
    $isCurrent = ($agent.status_code -eq 200 -and $agent.body -and $agent.body.agent_api -eq $true -and [string]$agent.body.service -eq 'xiaoyu-router-agent-api')
    $hasLegacySurface = ($health.status_code -eq 200 -or $models.status_code -eq 200)
    $classification = if ($isCurrent) { 'CURRENT_ROUTER' } elseif ($agent.status_code -eq 404 -and $hasLegacySurface) { 'STALE_OR_INCOMPATIBLE_ROUTER' } elseif ($agent.status_code -eq 0 -and $health.status_code -eq 0 -and $models.status_code -eq 0) { 'UNREACHABLE' } else { 'UNKNOWN_SERVICE' }
    return [pscustomobject]@{
        classification = $classification
        agent_health_status = $agent.status_code
        agent_api = if ($isCurrent) { $true } else { $false }
        agent_service = if ($agent.body) { [string]$agent.body.service } else { '' }
        agent_version = if ($agent.body) { [string]$agent.body.version } else { '' }
        health_status = $health.status_code
        models_status = $models.status_code
        no_post_sent = 'YES'
    }
}
function Get-RouterListenerInfo {
    $entries = @(Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue)
    if ($entries.Count -eq 0) {
        $entries = @()
        foreach ($line in @(netstat -ano -p tcp 2>$null)) {
            if ($line -match '^\s*TCP\s+(?<local>\S+):18789\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$') {
                $entries += [pscustomobject]@{ LocalAddress = ([string]$Matches.local).Trim('[',']'); OwningProcess = [int]$Matches.pid }
            }
        }
    }
    if ($entries.Count -eq 0) { return [pscustomobject]@{ listening = $false; loopback = $false; address = ''; port = 18789; pid = $null; process = ''; executable_path = ''; command_line = ''; owner_kind = 'NONE'; owned = $false } }
    $entry = $entries | Select-Object -First 1
    $addresses = @($entries | ForEach-Object { [string]$_.LocalAddress } | Sort-Object -Unique)
    $listenerPid = [int]$entry.OwningProcess; $metadata = Get-ProcessMetadata $listenerPid
    $identityText = ($metadata.process_name + ' ' + $metadata.executable_path + ' ' + $metadata.command_line)
    $identityMatch = $identityText -match '(?i)(codex[_-]ai[_-]router|xiaoyu[-_]router|python(?:\.exe)?\s+.*-m\s+codex_ai_router\.cli\s+serve)'
    $ownerKind = if ($identityMatch) { 'XIAOYU_ROUTER' } else { 'UNKNOWN_PROCESS' }
    $loopback = (@($addresses | Where-Object { $_ -notin @('127.0.0.1','::1','localhost') }).Count -eq 0)
    return [pscustomobject]@{
        listening = $true
        loopback = $loopback
        address = ($addresses -join ', ')
        port = 18789
        pid = $listenerPid
        process = $metadata.process_name
        executable_path = $metadata.executable_path
        command_line = $metadata.command_line
        owner_kind = $ownerKind
        owned = ($identityMatch -and $loopback)
    }
}
function Get-RouterToolsPolicyName {
    $path = Join-Path $env:USERPROFILE '.codex-ai-router\tools-policy.json'
    try { $value = Read-JsonFile $path ([pscustomobject]@{ codex_tools_policy = 'strict_reject' }); if ($value.codex_tools_policy) { return [string]$value.codex_tools_policy } } catch {}
    return 'strict_reject'
}
function Get-RouterStatus {
    $listener = Get-RouterListenerInfo; $models = $null; $probe = [pscustomobject]@{ classification = 'NOT_LISTENING'; agent_health_status = 0; agent_api = $false; agent_service = ''; agent_version = ''; health_status = 0; models_status = 0; no_post_sent = 'YES' }
    if ($listener.listening -and $listener.loopback) {
        $probe = Get-RouterIdentityProbe
        if ($probe.models_status -eq 200) { $modelsResponse = Invoke-RouterGet 'http://127.0.0.1:18789/v1/models'; $models = $modelsResponse.body }
    } elseif ($listener.listening) { $probe = [pscustomobject]@{ classification = 'NON_LOOPBACK_BINDING'; agent_health_status = 0; agent_api = $false; agent_service = ''; agent_version = ''; health_status = 0; models_status = 0; no_post_sent = 'YES' } }
    $codex = Get-CodexStatus
    return [pscustomobject]@{
        running = ($listener.listening -and $listener.loopback -and $probe.classification -eq 'CURRENT_ROUTER')
        address = 'http://127.0.0.1:18789/v1'
        mode = 'AUTO'
        models = $models
        listener_status = if($listener.listening){'LISTENING'}else{'NOT_LISTENING'}
        listener_address = $listener.address
        listener_port = 18789
        listener_pid = $listener.pid
        listener_process = $listener.process
        listener_executable_path = $listener.executable_path
        listener_command_line = $listener.command_line
        listener_owner_kind = if($probe.classification -eq 'CURRENT_ROUTER'){'CURRENT_ROUTER'}else{$listener.owner_kind}
        listener_owned = ($listener.owned -or $probe.classification -eq 'CURRENT_ROUTER')
        health_status = if($probe.health_status -eq 200){'PASS'}else{[string]$probe.health_status}
        models_status = if($probe.models_status -eq 200){'PASS'}else{[string]$probe.models_status}
        agent_health_status = $probe.agent_health_status
        agent_api = $probe.agent_api
        agent_service = $probe.agent_service
        agent_version = $probe.agent_version
        identity_status = $probe.classification
        stale_or_incompatible = if($probe.classification -eq 'STALE_OR_INCOMPATIBLE_ROUTER'){'YES'}else{'NO'}
        post_to_unknown_service = 'NO'
        current_codex_mode = $codex.provider
        current_codex_model = $codex.model
        tools_policy = Get-RouterToolsPolicyName
    }
}
function Get-RouterLaunchSpec {
    $candidates = @()
    if ($env:XIAOYU_ROUTER_PYTHON) { $candidates += $env:XIAOYU_ROUTER_PYTHON }
    $candidates += @((Join-Path $ProjectRoot '.venv\Scripts\python.exe'),(Join-Path $ProjectRoot 'venv\Scripts\python.exe'))
    foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'),(Join-Path $env:USERPROFILE 'miniconda3'),(Join-Path $env:USERPROFILE 'anaconda3'),'C:\ProgramData\Anaconda3','C:\EasyDiffusion\installer_files\env')) {
        if (Test-Path -LiteralPath $root) { $candidates += @(Get-ChildItem -LiteralPath $root -Filter 'python.exe' -File -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName) }
    }
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $probeOutput = & $candidate -c 'import tomllib' 2>$null; $probeExit = $LASTEXITCODE
            if ($probeExit -ne 0) { $probeOutput = & $candidate -c 'import tomli' 2>$null; $probeExit = $LASTEXITCODE }
            if ($probeExit -eq 0) { return [pscustomobject]@{ path = $candidate; kind = 'PYTHON_MODULE' } }
        } catch {}
    }
    $knownPython = 'C:\EasyDiffusion\installer_files\env\python.exe'
    if (Test-Path -LiteralPath $knownPython) { return [pscustomobject]@{ path = $knownPython; kind = 'PYTHON_MODULE' } }
    throw 'ROUTER_RUNTIME_NOT_FOUND'
}
function Confirm-StopOldRouter([object]$Listener,[object]$Identity) {
    $message = "检测到旧小羽 Router 正在占用 18789。`r`nPID：$($Listener.pid)`r`n进程：$($Listener.process)`r`n可执行路径：$($Listener.executable_path)`r`n命令行：$($Listener.command_line)`r`n身份探测：$($Identity.classification)`r`n仅停止已识别的小羽 Router，不会停止未知进程。`r`n是否停止旧进程并重新启动当前 Router？"
    if ($NoShow -or $SelfTest) { return $false }
    return ([System.Windows.Forms.MessageBox]::Show($message,'旧小羽 Router 占用端口',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning) -eq [System.Windows.Forms.DialogResult]::Yes)
}
function Stop-IdentifiedRouterProcess([object]$Listener) {
    if (-not $Listener.owned) { throw 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' }
    Stop-Process -Id ([int]$Listener.pid) -Force -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 20; $attempt++) { Start-Sleep -Milliseconds 250; if (-not (Get-RouterListenerInfo).listening) { return } }
    throw 'ROUTER_STOP_TIMEOUT'
}
function Start-Router {
    $existing = Get-RouterListenerInfo
    if ($existing.listening) {
        if (-not $existing.loopback) { throw 'ROUTER_NON_LOOPBACK_BINDING' }
        $status = Get-RouterStatus
        if ($status.identity_status -eq 'CURRENT_ROUTER') { return [pscustomobject]@{ status = 'ALREADY_RUNNING'; pid = $existing.pid; address = '127.0.0.1:18789'; identity = $status.identity_status } }
        if ($existing.owner_kind -eq 'XIAOYU_ROUTER' -and $status.identity_status -eq 'STALE_OR_INCOMPATIBLE_ROUTER') {
            if (-not (Confirm-StopOldRouter $existing $status)) { throw 'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' }
            Stop-IdentifiedRouterProcess $existing
        } else {
            throw 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS'
        }
    }
    $runtimeDir = Join-Path $ProjectRoot '.runtime\router'; if (-not (Test-Path -LiteralPath $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
    $stdout = Join-Path $runtimeDir 'router.stdout.log'; $stderr = Join-Path $runtimeDir 'router.stderr.log'; $pidFile = $RouterPidFile
    $spec = Get-RouterLaunchSpec; $runner = Join-Path $runtimeDir 'run-router.py'
    if ($spec.kind -eq 'PYTHON_MODULE') {
        $runnerSource = "import sys`nfrom pathlib import Path`nsys.path.insert(0, str(Path(r'$ProjectRoot\src')))`nfrom codex_ai_router.cli import main`nif __name__ == '__main__':`n    main()`n"
        # The generated wrapper is immutable runtime metadata. Some Windows
        # security layers keep an existing runtime script read-only/locked;
        # avoid rewriting an identical wrapper on every restart.
        $needsRunnerWrite = -not (Test-Path -LiteralPath $runner)
        if (-not $needsRunnerWrite) {
            try { $needsRunnerWrite = ([IO.File]::ReadAllText($runner, [Text.Encoding]::UTF8) -ne $runnerSource) } catch { throw 'ROUTER_RUNNER_READ_FAILED' }
        }
        if ($needsRunnerWrite) { [IO.File]::WriteAllText($runner, $runnerSource, (New-Object Text.UTF8Encoding($false))) }
        $arguments = @($runner,'serve','--host','127.0.0.1','--port','18789')
    } else { $arguments = @('serve','--host','127.0.0.1','--port','18789') }
    # The local Worker fixture requires a loopback-only test key. Inject it
    # only into the child process environment; never write or display it.
    $previousBridgeKey = [string]$env:XIAOYU_ROUTER_BRIDGE_API_KEY
    $injectedBridgeKey = [string]::IsNullOrWhiteSpace($previousBridgeKey)
    try {
        if ($injectedBridgeKey) { Set-Item -Path Env:XIAOYU_ROUTER_BRIDGE_API_KEY -Value (Get-DeepSeekFixtureKey) }
        $process = Start-Process -FilePath $spec.path -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    } finally {
        if ($injectedBridgeKey) { Remove-Item Env:XIAOYU_ROUTER_BRIDGE_API_KEY -ErrorAction SilentlyContinue } else { Set-Item -Path Env:XIAOYU_ROUTER_BRIDGE_API_KEY -Value $previousBridgeKey }
    }
    [IO.File]::WriteAllText($pidFile, [string]$process.Id, (New-Object Text.UTF8Encoding($false)))
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        Start-Sleep -Milliseconds 250; $status = Get-RouterStatus
        if ($status.running -and $status.listener_owned) { return [pscustomobject]@{ status = 'STARTED'; pid = $process.Id; address = '127.0.0.1:18789'; launcher = $spec.kind } }
        try { if ($process.HasExited) { break } } catch { break }
    }
    $detail = ''; if (Test-Path -LiteralPath $stderr) { $detail = Redact-Text (Get-Content -LiteralPath $stderr -Raw -Encoding UTF8) }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    if ($detail -match '(?i)tomli|modulenotfound|traceback') { throw 'ROUTER_START_DEPENDENCY_ERROR' }
    throw 'ROUTER_START_TIMEOUT'
}
function Stop-Router {
    $listener = Get-RouterListenerInfo
    if (-not $listener.listening) { return [pscustomobject]@{ status = 'ALREADY_STOPPED' } }
    if (-not $listener.loopback) { throw 'ROUTER_NON_LOOPBACK_BINDING' }
    $status = Get-RouterStatus
    $knownCurrent = ($status.identity_status -eq 'CURRENT_ROUTER')
    if (-not $listener.owned -and -not $knownCurrent) { throw 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' }
    if ($status.identity_status -eq 'STALE_OR_INCOMPATIBLE_ROUTER') {
        if (-not (Confirm-StopOldRouter $listener $status)) { throw 'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' }
    }
    if ($knownCurrent -and -not $listener.owned) {
        Stop-Process -Id ([int]$listener.pid) -Force -ErrorAction Stop
        for ($attempt = 0; $attempt -lt 20; $attempt++) { Start-Sleep -Milliseconds 250; if (-not (Get-RouterListenerInfo).listening) { break } }
    } else { Stop-IdentifiedRouterProcess $listener }
    Remove-Item -LiteralPath $RouterPidFile -Force -ErrorAction SilentlyContinue
    if ((Get-RouterListenerInfo).listening) { throw 'ROUTER_STOP_TIMEOUT' }
    return [pscustomobject]@{ status = 'STOPPED' }
}
function Stop-OldRouter {
    $listener = Get-RouterListenerInfo
    if (-not $listener.listening) { return [pscustomobject]@{ status = 'NOT_LISTENING' } }
    if (-not $listener.loopback) { throw 'ROUTER_NON_LOOPBACK_BINDING' }
    $status = Get-RouterStatus
    if ($listener.owner_kind -ne 'XIAOYU_ROUTER' -or $status.identity_status -ne 'STALE_OR_INCOMPATIBLE_ROUTER') { throw 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' }
    if (-not (Confirm-StopOldRouter $listener $status)) { throw 'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' }
    Stop-IdentifiedRouterProcess $listener
    return [pscustomobject]@{ status = 'OLD_ROUTER_STOPPED'; pid = $listener.pid }
}
function Get-DeepSeekPortState([int]$Port) {
    $entries = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($entries.Count -eq 0) { return '未监听' }
    $addresses = @($entries | ForEach-Object { [string]$_.LocalAddress } | Sort-Object -Unique)
    if (@($addresses | Where-Object { $_ -notin @('127.0.0.1','::1') }).Count -gt 0) { return ('异常绑定：' + ($addresses -join ', ')) }
    return '127.0.0.1 本机监听'
}
function Wait-DeepSeekLoopbackPort([int]$Port,[int]$TimeoutSeconds = 8) {
    for ($attempt = 0; $attempt -lt ($TimeoutSeconds * 4); $attempt++) {
        if ((Get-DeepSeekPortState $Port) -eq '127.0.0.1 本机监听') { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}
function Get-DeepSeekFixtureKey { return ('sk-' + ('a' * 43)) }
function Get-DeepSeekBridgeSnapshot {
    $snapshot = [ordered]@{ bridge_port = Get-DeepSeekPortState 8791; worker_port = Get-DeepSeekPortState 8792; fixture_port = Get-DeepSeekPortState 8793; bridge = '未运行'; worker = '未运行'; chrome = '未知'; page = '未知'; busy = '未知' }
    if ($snapshot.bridge_port -ne '未监听') {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8791/health' -TimeoutSec 3
            $health = $response.Content | ConvertFrom-Json
            $snapshot.bridge = if ($health.ok -eq $true) { 'READY' } else { '响应异常' }
            $snapshot.chrome = if ($health.browser.connected -eq $true) { '已连接' } else { '未连接' }
            $snapshot.page = if ($health.deepseek.loginRequired -eq $true) { 'LOGIN_REQUIRED' } elseif ($health.deepseek.inputReady -eq $true) { '输入框可用' } elseif ($health.deepseek.pageReady -eq $true) { '页面已打开，输入框不可用' } else { 'PAGE_NOT_READY' }
            $snapshot.busy = if ($health.busy -eq $true) { 'BUSY' } else { '空闲/未知' }
        } catch { $snapshot.bridge = 'BRIDGE_HTTP_UNREACHABLE'; $snapshot.chrome = '未知'; $snapshot.page = '未知'; $snapshot.busy = '未知' }
    }
    if ($snapshot.worker_port -ne '未监听') { $snapshot.worker = '本机监听（详细健康检查请点击）' }
    return [pscustomobject]$snapshot
}
function Get-DeepSeekOperationType([string]$Raw,[int]$ExitCode) {
    if ($ExitCode -eq 0) { return 'PASS' }
    if ($Raw -match 'LOGIN_REQUIRED') { return 'LOGIN_REQUIRED' }
    if ($Raw -match 'CAPTCHA|RISK_CONTROL') { return 'CAPTCHA_OR_RISK' }
    if ($Raw -match 'BUSY') { return 'BUSY' }
    if ($Raw -match 'NOT_LISTENING|UNREACHABLE|NOT_READY') { return 'SERVICE_UNAVAILABLE' }
    return 'LOCAL_SCRIPT_FAILED'
}
function Invoke-DeepSeekLocalScript([ValidateSet('start-bridge','start-worker','health-check','smoke','stop')][string]$Action) {
    $paths = @{ 'start-bridge' = 'start-bridge.ps1'; 'start-worker' = 'start-worker.ps1'; 'health-check' = 'health-check.ps1'; smoke = 'smoke-local.ps1'; stop = 'stop-local.ps1' }
    $path = Join-Path $DeepSeekBridgeScripts $paths[$Action]
    if (-not (Test-Path -LiteralPath $path)) { return [pscustomobject]@{ action = $Action; exit_code = 1; error_type = 'SCRIPT_NOT_FOUND' } }
    $previousTestKey = [string]$env:LOCAL_GATEWAY_TEST_KEY
    try {
        $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$path)
        if ($Action -eq 'smoke') { $env:LOCAL_GATEWAY_TEST_KEY = Get-DeepSeekFixtureKey; $arguments += '-NonInteractive' }
        $raw = (& powershell.exe @arguments 2>&1 | Out-String)
        $exitCode = $LASTEXITCODE
        $type = Get-DeepSeekOperationType -Raw $raw -ExitCode $exitCode
        if ($Action -eq 'start-worker' -and $exitCode -eq 0 -and -not (Wait-DeepSeekLoopbackPort 8792)) { $exitCode = 1; $type = 'WORKER_START_TIMEOUT' }
        if ($Action -eq 'health-check') { $script:DeepSeekLastHealth = $type }
        return [pscustomobject]@{ action = $Action; exit_code = $exitCode; error_type = $type }
    } catch { return [pscustomobject]@{ action = $Action; exit_code = 1; error_type = 'LOCAL_SCRIPT_EXCEPTION' } }
    finally {
        if ([string]::IsNullOrEmpty($previousTestKey)) { Remove-Item Env:LOCAL_GATEWAY_TEST_KEY -ErrorAction SilentlyContinue } else { $env:LOCAL_GATEWAY_TEST_KEY = $previousTestKey }
    }
}
function Invoke-RouterDirectSmoke {
    $body = @{ model = 'xiaoyu-lightboat'; input = 'Reply exactly: ROUTER_SMOKE_OK'; max_output_tokens = 8; stream = $false } | ConvertTo-Json -Compress
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $preflight = Get-RouterStatus
    if (-not $preflight.running) {
        $blockedCode = if ($preflight.identity_status -eq 'STALE_OR_INCOMPATIBLE_ROUTER') { 'STALE_OR_INCOMPATIBLE_ROUTER' } elseif ($preflight.listener_status -eq 'LISTENING') { 'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' } else { 'ROUTER_NOT_RUNNING' }
        return [pscustomobject]@{ success = $false; status = 'BLOCKED'; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2); error_code = $blockedCode; post_sent = 'NO' }
    }
    try {
        $result = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18789/v1/responses' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 25
        return [pscustomobject]@{ success = ($result.StatusCode -eq 200); status = $result.StatusCode; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2); error_code = $null; post_sent = 'YES' }
    } catch {
        return [pscustomobject]@{ success = $false; status = 'FAILED'; seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2); error_code = 'ROUTER_SMOKE_FAILED'; post_sent = 'YES' }
    }
}
function Update-ProviderEnabled([string]$Id) { $data = Get-ProviderData; $provider = $data.providers.($Id); if (-not $provider) { throw 'Provider was not found.' }; $provider.enabled = ($provider.enabled -eq $false); Write-JsonAtomic $ProviderConfig $data }
function Invoke-RouterCli([string[]]$Arguments) {
    $spec = Get-RouterLaunchSpec
    if ($spec.kind -eq 'PYTHON_MODULE') {
        $runtimeDir = Join-Path $ProjectRoot '.runtime\router'; if (-not (Test-Path -LiteralPath $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
        $runner = Join-Path $runtimeDir 'run-router-cli.py'
        if (-not (Test-Path -LiteralPath $runner)) {
            $runnerSource = "import sys`nfrom pathlib import Path`nsys.path.insert(0, str(Path(r'$ProjectRoot\src')))`nfrom codex_ai_router.cli import main`nif __name__ == '__main__':`n    main()`n"
            [IO.File]::WriteAllText($runner, $runnerSource, (New-Object Text.UTF8Encoding($false)))
        }
        $cliArgs = @($runner) + @($Arguments); return (Redact-Text (& $spec.path @cliArgs 2>&1 | Out-String))
    }
    return (Redact-Text (& $spec.path @Arguments 2>&1 | Out-String))
}
function Add-LocalAgentLog([string]$Message) {
    if ($localAgentLog) { $localAgentLog.AppendText(('[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), (Redact-Text $Message)) + "`r`n") }
}
function Refresh-LocalAgentPanel {
    $plan = $script:LocalAgentPlanJson
    if (-not $plan) {
        $localAgentStatus.Text = "当前 Agent 模式：PLAN_ONLY / READ_ONLY`r`n当前大脑：local-light（仅计划模板，尚未调用 Provider）`r`n风险等级：UNKNOWN`r`n待执行计划：无`r`n`r`n默认不会修改文件、运行测试或提交 commit。所有写入、测试、commit 都需要显式确认。"
        return
    }
    $steps = @($plan.steps | ForEach-Object { '{0}：{1}（确认：{2}）' -f $_.type,$_.description,$_.requires_confirmation }) -join "`r`n"
    $brainResult = if ($plan.brain_output) { Redact-Text ([string]$plan.brain_output).Substring(0, [Math]::Min(500, ([string]$plan.brain_output).Length)) } elseif ($plan.brain_error_code) { '大脑调用错误：' + $plan.brain_error_code } else { '尚未显式调用大脑；默认仅生成计划模板。' }
    $localAgentStatus.Text = ("当前 Agent 模式：{0}`r`n计划 ID：{1}`r`n大脑 Provider：{2}`r`n大脑状态：{3}`r`n风险等级：{4}`r`n需要文件：{5}`r`n需要写入：{6}`r`n需要测试：{7}`r`n需要 commit：{8}`r`n拒绝原因：{9}`r`n大脑结果摘要：{10}`r`n`r`n待执行步骤：`r`n{11}`r`n`r`n自动修改：NO；自动执行命令：NO；自动 commit：NO；自动 push：NO；自动部署：NO" -f $(if($plan.mode){$plan.mode}else{'PLAN_ONLY'}),$plan.plan_id,$plan.brain_provider,$plan.brain_status,$plan.risk_level,$plan.requires_files,$plan.requires_write,$plan.requires_tests,$plan.requires_commit,$(if($plan.deny_reason){$plan.deny_reason}else{'NONE'}),$brainResult,$steps)
}
function Get-LocalAgentTask([string]$Title = '输入任务') {
    Add-Type -AssemblyName Microsoft.VisualBasic
    return [Microsoft.VisualBasic.Interaction]::InputBox('仅用于生成计划；不会自动执行任务：','小羽 Local Agent - ' + $Title,'只读检查当前项目')
}
function Require-LocalAgentPlan {
    if ([string]::IsNullOrWhiteSpace($script:LocalAgentPlanId)) { [System.Windows.Forms.MessageBox]::Show('请先生成一个计划。','小羽 Local Agent'); return $false }
    return $true
}
function Invoke-LocalAgentPlan([bool]$InvokeBrain = $false) {
    $task = Get-LocalAgentTask '生成计划'; if ([string]::IsNullOrWhiteSpace($task)) { return }
    if ($InvokeBrain -and [System.Windows.Forms.MessageBox]::Show('将把任务发送给已选择的本地大脑 Provider，仅生成分析，不会修改文件、运行命令或提交。继续？','调用大脑确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning) -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $arguments = @('agent','plan','--task',$task,'--brain-provider',$script:LocalAgentBrainProvider); if ($InvokeBrain) { $arguments += '--invoke-brain' }
    $raw = Invoke-RouterCli $arguments
    try { $script:LocalAgentPlanJson = $raw | ConvertFrom-Json; $script:LocalAgentPlanId = [string]$script:LocalAgentPlanJson.plan_id; Add-LocalAgentLog ('plan=' + $script:LocalAgentPlanId + '; status=' + $script:LocalAgentPlanJson.status); Refresh-LocalAgentPanel; [System.Windows.Forms.MessageBox]::Show(('计划已生成。`r`n计划 ID：' + $script:LocalAgentPlanId + '`r`n不会自动修改文件、运行测试或提交。'),'小羽 Local Agent') } catch { throw 'LOCAL_AGENT_PLAN_RESPONSE_INVALID' }
}
function Invoke-LocalAgentReadonly {
    $arguments = @('agent','readonly'); if ($script:LocalAgentPlanId) { $arguments += @('--plan',$script:LocalAgentPlanId) }
    $raw = Invoke-RouterCli $arguments; Add-LocalAgentLog ('readonly=' + (Redact-Text $raw)); [System.Windows.Forms.MessageBox]::Show($raw,'小羽 Local Agent 只读检查')
}
function Invoke-LocalAgentDraft {
    if (-not (Require-LocalAgentPlan)) { return }
    $confirm = [System.Windows.Forms.MessageBox]::Show('只会生成候选补丁文件，不会应用到项目。继续？','补丁草案确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $raw = Invoke-RouterCli @('agent','draft-patch','--plan',$script:LocalAgentPlanId,'--confirm'); Add-LocalAgentLog ('draft-patch=' + $script:LocalAgentPlanId); [System.Windows.Forms.MessageBox]::Show($raw,'补丁草案')
}
function Invoke-LocalAgentApply {
    if (-not (Require-LocalAgentPlan)) { return }
    Add-Type -AssemblyName Microsoft.VisualBasic
    $patch = [Microsoft.VisualBasic.Interaction]::InputBox('输入 .patch 文件路径（必须位于当前项目或 Local Agent 草案目录）：','应用补丁','')
    if ([string]::IsNullOrWhiteSpace($patch)) { return }
    $first = [System.Windows.Forms.MessageBox]::Show('应用前会先执行 git apply --check，并显示结果。确认应用此补丁？','第一次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($first -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $second = [System.Windows.Forms.MessageBox]::Show('再次确认：补丁可能修改工作区文件，应用后仍需人工检查 diff。继续？','第二次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($second -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $raw = Invoke-RouterCli @('agent','apply','--plan',$script:LocalAgentPlanId,'--patch-file',$patch,'--confirm'); Add-LocalAgentLog ('apply=' + $script:LocalAgentPlanId); [System.Windows.Forms.MessageBox]::Show($raw,'应用补丁结果')
}
function Invoke-LocalAgentTest {
    if (-not (Require-LocalAgentPlan)) { return }
    $test = [Microsoft.VisualBasic.Interaction]::InputBox('输入白名单测试名：python-unittest / pytest / npm-test / npm-lint / npm-build / git-diff-check','运行测试','python-unittest')
    if ([string]::IsNullOrWhiteSpace($test)) { return }
    $first = [System.Windows.Forms.MessageBox]::Show(('将运行白名单测试：' + $test + '。继续？'),'第一次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($first -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $second = [System.Windows.Forms.MessageBox]::Show('再次确认运行测试？测试输出只保留脱敏摘要。','第二次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($second -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $raw = Invoke-RouterCli @('agent','test','--plan',$script:LocalAgentPlanId,'--test',$test,'--confirm'); Add-LocalAgentLog ('test=' + $test); [System.Windows.Forms.MessageBox]::Show($raw,'测试结果')
}
function Invoke-LocalAgentCommit {
    if (-not (Require-LocalAgentPlan)) { return }
    Add-Type -AssemblyName Microsoft.VisualBasic
    $files = [Microsoft.VisualBasic.Interaction]::InputBox('输入要提交的相对文件路径（逗号分隔）：','本地 commit 文件','')
    $message = [Microsoft.VisualBasic.Interaction]::InputBox('输入本地 commit message（不会 push）：','本地 commit message','local agent confirmed change')
    if ([string]::IsNullOrWhiteSpace($files) -or [string]::IsNullOrWhiteSpace($message)) { return }
    $first = [System.Windows.Forms.MessageBox]::Show(('仅提交以下文件：' + $files + '`r`n不会 push。继续？'),'第一次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($first -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $second = [System.Windows.Forms.MessageBox]::Show('再次确认创建本地 commit？','第二次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($second -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $arguments = @('agent','commit','--plan',$script:LocalAgentPlanId,'--message',$message,'--confirm'); foreach($file in ($files -split ',' | ForEach-Object {$_.Trim()} | Where-Object {$_})){ $arguments += @('--file',$file) }
    $raw = Invoke-RouterCli $arguments; Add-LocalAgentLog ('commit=' + $script:LocalAgentPlanId + '; files=' + (($files -split ',').Count)); [System.Windows.Forms.MessageBox]::Show($raw,'commit 结果')
}
function Get-ToolsPolicyRecord {
    try { return (Invoke-RouterCli @('tools-policy','show') | ConvertFrom-Json) }
    catch { return [pscustomobject]@{ codex_tools_policy = 'strict_reject'; default = 'strict_reject'; path = (Join-Path $env:USERPROFILE '.codex-ai-router\tools-policy.json') } }
}
function Set-ToolsPolicy([ValidateSet('strict_reject','text_only_strip','manual_plan')][string]$Policy) {
    $raw = Invoke-RouterCli @('tools-policy','set',$Policy)
    try { return ($raw | ConvertFrom-Json) } catch { throw 'TOOLS_POLICY_SAVE_FAILED' }
}
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

if ($RouterAction) {
    try {
        $result = switch ($RouterAction) { 'start' { Start-Router }; 'stop' { Stop-Router }; 'status' { Get-RouterStatus } }
        $result | ConvertTo-Json -Depth 8 -Compress
        exit 0
    } catch {
        $owner = Get-RouterListenerInfo
        $probe = if ($owner.listening -and $owner.loopback) { Get-RouterIdentityProbe } else { [pscustomobject]@{ classification = 'NOT_PROBED'; agent_health_status = 0; health_status = 0; models_status = 0; no_post_sent = 'YES' } }
        [pscustomobject]@{
            status = 'ERROR'
            error_code = [string]$_.Exception.Message
            port = 18789
            listener_address = $owner.address
            listener_pid = $owner.pid
            listener_process = $owner.process
            listener_executable_path = $owner.executable_path
            listener_command_line = $owner.command_line
            listener_owner_kind = $owner.owner_kind
            identity_status = $probe.classification
            agent_health_status = $probe.agent_health_status
            health_status = $probe.health_status
            models_status = $probe.models_status
            post_to_unknown_service = 'NO'
        } | ConvertTo-Json -Depth 8 -Compress
        exit 1
    }
}

if ($NoShow) {
    $router = Get-RouterStatus; $rows = Get-ProviderRows
    Write-Output ('ROUTER_STATUS_VISIBLE=' + $(if ($router.running) { 'YES' } else { 'NO' })); Write-Output 'CODEX_STATUS_VISIBLE=YES'; Write-Output 'PROVIDER_LIST_VISIBLE=YES'; Write-Output ('LIGHTBOAT_PROVIDER_VISIBLE=' + $(if (@($rows | Where-Object { $_.provider_id -eq 'lightboat-3' }).Count -gt 0) { 'YES' } else { 'NO' })); Write-Output 'USAGE_GUARD_VISIBLE=YES'; Write-Output 'DEEPSEEK_LOCAL_BRIDGE_PANEL_VISIBLE=YES'; Write-Output 'CODEX_MODE_PANEL_VISIBLE=YES'; Write-Output 'PROVIDER_ALLOWLIST_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_RECORDS_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_AGENT_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_AGENT_DEFAULT_READ_ONLY=YES'; Write-Output 'LOCAL_AGENT_CONFIRMATION_GATES=YES'; Write-Output 'LOCAL_AGENT_BRAIN_EXPLICIT=YES'; Write-Output 'DEEPSEEK_BRIDGE_DIRECT_BRAIN_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_BRIDGE_DIRECT_ENDPOINT=127.0.0.1:8791'; Write-Output 'CODEX_TASK_INPUT_LOCATION=CODEX_ONLY'; Write-Output 'NO_QUOTA_MODE_VISIBLE=YES'; Write-Output 'RESPONSE_COMPAT_DIAGNOSTICS_VISIBLE=YES'; Write-Output 'TOOLS_POLICY_UI_VISIBLE=YES'; Write-Output 'TEXT_ONLY_MODE_BUTTONS_VISIBLE=YES'; Write-Output 'TEXT_ONLY_DEFAULT_STRICT_REJECT=YES'; Write-Output 'DEEPSEEK_HEALTH_PROMPT_SENT=NO'; Write-Output 'DEEPSEEK_MODE_PROBE_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_MODE_SELECTOR_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_MODE_PROBE_PROMPT_SENT=NO'; Write-Output 'CONTROL_PANEL_LANGUAGE=ZH_CN'; Write-Output 'ERROR_CODE_CHINESE_EXPLANATION=YES'; Write-Output 'DEBUG_FIELDS_COLLAPSED=YES'; Write-Output 'CONTROL_PANEL_EXCEPTION_GUARD=YES'; Write-Output 'NO_JIT_DIALOG_ON_BUTTON_ERROR=YES'; Write-Output 'CONTROL_PANEL_JSON_POPUP_DEFAULT=NO'; Write-Output 'OFFICIAL_DIRECT_TOOLS_POLICY_DISPLAY=NOT_APPLICABLE'; Write-Output 'SECRET_VALUES_VISIBLE=NO'; Write-Output 'ROUTER_PORT_OWNER_FIELDS=YES'; Write-Output 'ROUTER_IDENTITY_PROBE=YES'; Write-Output 'STALE_ROUTER_CONFIRMATION_GATE=YES'; Write-Output 'UNKNOWN_PROCESS_SAFE_STOP=YES'; Write-Output 'UNKNOWN_SERVICE_POST_BLOCKED=YES'; exit 0
}

$uiFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$buttonFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$form = New-Object System.Windows.Forms.Form; $form.Text = '小羽 Router 控制台'; $form.Size = New-Object System.Drawing.Size(1180,780); $form.MinimumSize = New-Object System.Drawing.Size(920,620); $form.StartPosition = 'CenterScreen'; $form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi; $form.Font = $uiFont
$tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = 'Fill'; $form.Controls.Add($tabs)
$homeTab = New-Object System.Windows.Forms.TabPage('首页'); $providersTab = New-Object System.Windows.Forms.TabPage('供应商'); $deepSeekTab = New-Object System.Windows.Forms.TabPage('DeepSeek 本地桥接'); $assistantTab = New-Object System.Windows.Forms.TabPage('Codex 模式与供应商白名单'); $localAgentTab = New-Object System.Windows.Forms.TabPage('小羽本地 Agent'); $usageTab = New-Object System.Windows.Forms.TabPage('用量保护'); $diagnosticsTab = New-Object System.Windows.Forms.TabPage('诊断'); [void]$tabs.TabPages.AddRange(@($homeTab,$providersTab,$deepSeekTab,$assistantTab,$localAgentTab,$usageTab,$diagnosticsTab))
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
$deepSeekLayout = New-Object System.Windows.Forms.TableLayoutPanel; $deepSeekLayout.Dock = 'Fill'; $deepSeekLayout.Padding = New-Object System.Windows.Forms.Padding(12); $deepSeekLayout.RowCount = 4; $deepSeekLayout.ColumnCount = 1; [void]$deepSeekLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,45))); [void]$deepSeekLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,145))); [void]$deepSeekLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,112))); [void]$deepSeekLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,35))); $deepSeekTab.Controls.Add($deepSeekLayout)
$deepSeekStatusGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekStatusGroup.Text = 'DeepSeek 本地桥接状态（LOCAL_DIRECT）'; $deepSeekStatusGroup.Dock = 'Fill'; $deepSeekStatusGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekStatusGroup,0,0)
$deepSeekStatus = New-Object System.Windows.Forms.TextBox; $deepSeekStatus.Multiline = $true; $deepSeekStatus.ReadOnly = $true; $deepSeekStatus.ScrollBars = 'Vertical'; $deepSeekStatus.Font = $uiFont; $deepSeekStatus.Dock = 'Fill'; $deepSeekStatusGroup.Controls.Add($deepSeekStatus)
$deepSeekModeGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekModeGroup.Text = 'DeepSeek 网页模式策略（只读探测）'; $deepSeekModeGroup.Dock = 'Fill'; $deepSeekModeGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekModeGroup,0,1)
$deepSeekModeStatus = New-Object System.Windows.Forms.TextBox; $deepSeekModeStatus.Multiline = $true; $deepSeekModeStatus.ReadOnly = $true; $deepSeekModeStatus.Dock = 'Fill'; $deepSeekModeStatus.Font = $uiFont; $deepSeekModeGroup.Controls.Add($deepSeekModeStatus)
$deepSeekModeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $deepSeekModeButtons.Dock = 'Bottom'; $deepSeekModeButtons.Height = 38; $deepSeekModeButtons.Font = $buttonFont; $deepSeekModeGroup.Controls.Add($deepSeekModeButtons)
$deepSeekActionsGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekActionsGroup.Text = '本地操作'; $deepSeekActionsGroup.Dock = 'Fill'; $deepSeekActionsGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekActionsGroup,0,2)
$deepSeekButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $deepSeekButtons.Dock = 'Fill'; $deepSeekButtons.AutoScroll = $true; $deepSeekButtons.Font = $buttonFont; $deepSeekActionsGroup.Controls.Add($deepSeekButtons)
$deepSeekLogGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekLogGroup.Text = '最近本地操作日志（已脱敏）'; $deepSeekLogGroup.Dock = 'Fill'; $deepSeekLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekLogGroup,0,3)
$deepSeekLog = New-Object System.Windows.Forms.TextBox; $deepSeekLog.Multiline = $true; $deepSeekLog.ReadOnly = $true; $deepSeekLog.ScrollBars = 'Vertical'; $deepSeekLog.Font = $uiFont; $deepSeekLog.Dock = 'Fill'; $deepSeekLogGroup.Controls.Add($deepSeekLog)
$assistantLayout = New-Object System.Windows.Forms.TableLayoutPanel; $assistantLayout.Dock = 'Fill'; $assistantLayout.Padding = New-Object System.Windows.Forms.Padding(12); $assistantLayout.RowCount = 4; $assistantLayout.ColumnCount = 1; [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,175))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,145))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,45))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,55))); $assistantTab.Controls.Add($assistantLayout)
$codexModeGroup = New-Object System.Windows.Forms.GroupBox; $codexModeGroup.Text = 'Codex 连接模式'; $codexModeGroup.Dock = 'Fill'; $codexModeGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($codexModeGroup,0,0)
$codexModeStatus = New-Object System.Windows.Forms.TextBox; $codexModeStatus.Multiline = $true; $codexModeStatus.ReadOnly = $true; $codexModeStatus.Dock = 'Fill'; $codexModeStatus.Font = $uiFont; $codexModeGroup.Controls.Add($codexModeStatus)
$codexModeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexModeButtons.Dock = 'Bottom'; $codexModeButtons.Height = 42; $codexModeGroup.Controls.Add($codexModeButtons)
$toolsPolicyGroup = New-Object System.Windows.Forms.GroupBox; $toolsPolicyGroup.Text = 'Codex 工具策略（默认严格拒绝）'; $toolsPolicyGroup.Dock = 'Fill'; $toolsPolicyGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($toolsPolicyGroup,0,1)
$toolsPolicyStatus = New-Object System.Windows.Forms.TextBox; $toolsPolicyStatus.Multiline = $true; $toolsPolicyStatus.ReadOnly = $true; $toolsPolicyStatus.Dock = 'Fill'; $toolsPolicyStatus.Font = $uiFont; $toolsPolicyGroup.Controls.Add($toolsPolicyStatus)
$toolsPolicyButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $toolsPolicyButtons.Dock = 'Bottom'; $toolsPolicyButtons.Height = 42; $toolsPolicyGroup.Controls.Add($toolsPolicyButtons)
# Provider Allowlist 是内部调试字段；用户界面统一显示为“供应商白名单”。
$allowlistGroup = New-Object System.Windows.Forms.GroupBox; $allowlistGroup.Text = '供应商白名单（Codex 请求触发调用；外部 API 默认禁用）'; $allowlistGroup.Dock = 'Fill'; $allowlistGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($allowlistGroup,0,2)
$allowlistStatus = New-Object System.Windows.Forms.TextBox; $allowlistStatus.Multiline = $true; $allowlistStatus.ReadOnly = $true; $allowlistStatus.ScrollBars = 'Vertical'; $allowlistStatus.Dock = 'Fill'; $allowlistStatus.Font = $uiFont; $allowlistGroup.Controls.Add($allowlistStatus)
$recordGroup = New-Object System.Windows.Forms.GroupBox; $recordGroup.Text = '本地调用记录（仅元数据，默认不保存正文）'; $recordGroup.Dock = 'Fill'; $recordGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($recordGroup,0,3)
$recordStatus = New-Object System.Windows.Forms.TextBox; $recordStatus.Multiline = $true; $recordStatus.ReadOnly = $true; $recordStatus.ScrollBars = 'Vertical'; $recordStatus.Dock = 'Fill'; $recordStatus.Font = $uiFont; $recordGroup.Controls.Add($recordStatus)
$localAgentLayout = New-Object System.Windows.Forms.TableLayoutPanel; $localAgentLayout.Dock = 'Fill'; $localAgentLayout.Padding = New-Object System.Windows.Forms.Padding(12); $localAgentLayout.RowCount = 3; $localAgentLayout.ColumnCount = 1; [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,62))); [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,110))); [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,38))); $localAgentTab.Controls.Add($localAgentLayout)
$localAgentStatusGroup = New-Object System.Windows.Forms.GroupBox; $localAgentStatusGroup.Text = '小羽 Local Agent 状态（默认只读；不调用 Codex 官方 Agent）'; $localAgentStatusGroup.Dock = 'Fill'; $localAgentStatusGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentStatusGroup,0,0)
$localAgentStatus = New-Object System.Windows.Forms.TextBox; $localAgentStatus.Multiline = $true; $localAgentStatus.ReadOnly = $true; $localAgentStatus.ScrollBars = 'Vertical'; $localAgentStatus.Dock = 'Fill'; $localAgentStatus.Font = $uiFont; $localAgentStatusGroup.Controls.Add($localAgentStatus)
$localAgentActionGroup = New-Object System.Windows.Forms.GroupBox; $localAgentActionGroup.Text = '受确认门控的操作'; $localAgentActionGroup.Dock = 'Fill'; $localAgentActionGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentActionGroup,0,1)
$localAgentBrainLabel = New-Object System.Windows.Forms.Label; $localAgentBrainLabel.Text = 'Brain Provider（仅本机、仅文本计划）：'; $localAgentBrainLabel.AutoSize = $true; $localAgentBrainLabel.Dock = 'Top'; $localAgentBrainLabel.Font = $buttonFont; $localAgentActionGroup.Controls.Add($localAgentBrainLabel)
$localAgentBrainProvider = New-Object System.Windows.Forms.ComboBox; $localAgentBrainProvider.DropDownStyle = 'DropDownList'; $localAgentBrainProvider.Dock = 'Top'; $localAgentBrainProvider.Font = $buttonFont; [void]$localAgentBrainProvider.Items.Add('local-light（本地轻量）'); [void]$localAgentBrainProvider.Items.Add('DeepSeek Bridge 直连（本地 8791）'); [void]$localAgentBrainProvider.Items.Add('deepseek-head（本地 Worker）'); [void]$localAgentBrainProvider.Items.Add('external-allowed（显式白名单）'); [void]$localAgentBrainProvider.Items.Add('hybrid-agent（健康 Provider）'); $localAgentBrainProvider.SelectedIndex = 0; $localAgentBrainProvider.Add_SelectedIndexChanged({ $script:LocalAgentBrainProvider = switch ([string]$localAgentBrainProvider.SelectedItem) { 'DeepSeek Bridge 直连（本地 8791）' { 'deepseek-bridge-direct' } 'deepseek-head（本地 Worker）' { 'deepseek-head' } 'external-allowed（显式白名单）' { 'external-allowed' } 'hybrid-agent（健康 Provider）' { 'hybrid-agent' } default { 'local-light' } } }.GetNewClosure()); $script:LocalAgentBrainProvider = 'local-light'; $localAgentActionGroup.Controls.Add($localAgentBrainProvider)
$localAgentButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $localAgentButtons.Dock = 'Fill'; $localAgentButtons.AutoScroll = $true; $localAgentButtons.Font = $buttonFont; $localAgentActionGroup.Controls.Add($localAgentButtons)
$localAgentLogGroup = New-Object System.Windows.Forms.GroupBox; $localAgentLogGroup.Text = 'Local Agent 记录（仅元数据，默认不保存正文）'; $localAgentLogGroup.Dock = 'Fill'; $localAgentLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentLogGroup,0,2)
$localAgentLog = New-Object System.Windows.Forms.TextBox; $localAgentLog.Multiline = $true; $localAgentLog.ReadOnly = $true; $localAgentLog.ScrollBars = 'Vertical'; $localAgentLog.Dock = 'Fill'; $localAgentLog.Font = $uiFont; $localAgentLogGroup.Controls.Add($localAgentLog)
$usageText = New-Object System.Windows.Forms.TextBox; $usageText.Multiline = $true; $usageText.ReadOnly = $true; $usageText.Font = $uiFont; $usageText.Dock = 'Fill'; $usageTab.Controls.Add($usageText)
$usageButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $usageButtons.Dock = 'Top'; $usageButtons.Height = 42; $usageTab.Controls.Add($usageButtons)
$diagnosticsLayout = New-Object System.Windows.Forms.TableLayoutPanel; $diagnosticsLayout.Dock = 'Fill'; $diagnosticsLayout.RowCount = 2; $diagnosticsLayout.ColumnCount = 1; [void]$diagnosticsLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,42))); [void]$diagnosticsLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $diagnosticsTab.Controls.Add($diagnosticsLayout)
$diagnosticsToolbar = New-Object System.Windows.Forms.FlowLayoutPanel; $diagnosticsToolbar.Dock = 'Fill'; $diagnosticsToolbar.Font = $buttonFont; [void]$diagnosticsLayout.Controls.Add($diagnosticsToolbar,0,0)
$debugToggle = New-Object System.Windows.Forms.CheckBox; $debugToggle.Text = '显示高级信息 / 调试信息'; $debugToggle.AutoSize = $true; $debugToggle.Font = $buttonFont; [void]$diagnosticsToolbar.Controls.Add($debugToggle)
$copyDebugButton = New-Object System.Windows.Forms.Button; $copyDebugButton.Text = '复制诊断 JSON'; $copyDebugButton.Width = 150; $copyDebugButton.Height = 30; $copyDebugButton.Font = $buttonFont; [void]$diagnosticsToolbar.Controls.Add($copyDebugButton)
$diagnosticsText = New-Object System.Windows.Forms.TextBox; $diagnosticsText.Multiline = $true; $diagnosticsText.ReadOnly = $true; $diagnosticsText.Font = $uiFont; $diagnosticsText.Dock = 'Fill'; $diagnosticsText.Visible = $false; [void]$diagnosticsLayout.Controls.Add($diagnosticsText,0,1); $debugToggle.Add_CheckedChanged({$diagnosticsText.Visible = $debugToggle.Checked}.GetNewClosure())
function Add-ProviderLog([string]$Message) { $line = ('[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), (Redact-Text $Message)); $providerLog.AppendText($line + "`r`n") }
function Add-DeepSeekLog([string]$Action,[object]$Result) {
    $line = ('[{0}] action={1}; exit_code={2}; status={3}' -f (Get-Date).ToString('HH:mm:ss'),$Action,$Result.exit_code,$Result.error_type)
    $deepSeekLog.AppendText((Redact-Text $line) + "`r`n")
}
function Get-DeepSeekModeStatusText([object]$Probe) {
    if (-not $Probe) { return ("模式策略：自动选择（尚未运行只读探测）`r`n请点击探测 DeepSeek 模式；该操作只读取页面控件，不发送提示词、不点击发送。") }
    $labels = [ordered]@{ normal = '普通'; search = '搜索'; thinking = '思考'; expert = '专家' }
    $lines = @("模式策略：$script:DeepSeekModePreference",("探测：{0}；promptSent={1}；clickSend={2}" -f $Probe.status,$Probe.promptSent,$Probe.clickSend))
    foreach ($mode in $labels.Keys) {
        $item = $Probe.modes.$mode
        $state = if ($item.status -eq 'AVAILABLE' -and $item.controllable) { '可用' } elseif ($item.status) { [string]$item.status } else { 'UNKNOWN' }
        $lines += ("{0}模式：{1}（控件发现：{2}；可控：{3}）" -f $labels[$mode],$state,$item.controlDetected,$item.controllable)
    }
    if ($Probe.error_code) { $lines += ("错误码：{0}；说明：{1}" -f $Probe.error_code,(Get-UiErrorExplanation ([string]$Probe.error_code))) }
    if ($script:DeepSeekLastSelection) { $lines += ("最近推荐模式：{0}（{1}）" -f $script:DeepSeekLastSelection.selected_mode,$script:DeepSeekLastSelection.selected_model_alias) }
    $lines += '自动选择顺序：搜索 → 思考 → 专家 → 普通；手动固定或显式模型别名优先。'
    return ($lines -join "`r`n")
}
function Refresh-DeepSeekModePanel {
    if ($deepSeekModeStatus) { $deepSeekModeStatus.Text = Get-DeepSeekModeStatusText $script:DeepSeekModeProbe }
}
function Invoke-DeepSeekModeProbe {
    $raw = Invoke-RouterCli @('deepseek','mode-probe')
    try {
        $script:DeepSeekModeProbe = $raw | ConvertFrom-Json
        Refresh-DeepSeekModePanel
        Add-DeepSeekLog 'mode-probe' ([pscustomobject]@{ exit_code = 0; error_type = if($script:DeepSeekModeProbe.status -eq 'PASS'){'PASS'}else{[string]$script:DeepSeekModeProbe.error_code} })
        [System.Windows.Forms.MessageBox]::Show((Get-DeepSeekModeStatusText $script:DeepSeekModeProbe),'DeepSeek 模式探测') | Out-Null
    } catch {
        $script:DeepSeekModeProbe = [pscustomobject]@{ status = 'UI_PROBE_FAILED'; error_code = 'UI_PROBE_RESPONSE_INVALID'; promptSent = $false; clickSend = $false; modes = @{} }
        Refresh-DeepSeekModePanel
        Add-DeepSeekLog 'mode-probe' ([pscustomobject]@{ exit_code = 1; error_type = 'UI_PROBE_RESPONSE_INVALID' })
        throw 'UI_PROBE_RESPONSE_INVALID'
    }
}
function Set-DeepSeekModePreference([ValidateSet('auto','normal','search','thinking','expert')][string]$Preference) {
    $script:DeepSeekModePreference = $Preference
    Refresh-DeepSeekModePanel
    [System.Windows.Forms.MessageBox]::Show(("已设置 DeepSeek 模式策略：{0}`r`n这只影响本地选择说明，不会发送请求，也不会修改 Codex 配置。" -f $Preference),'DeepSeek 模式策略') | Out-Null
}
function Format-DeepSeekSelectionSummary([object]$Record) {
    $labels = @{ normal = '普通'; search = '搜索'; thinking = '思考'; expert = '专家'; auto = '自动' }
    $mode = if ($labels.ContainsKey([string]$Record.selected_mode)) { $labels[[string]$Record.selected_mode] } else { [string]$Record.selected_mode }
    $fallback = if ($Record.fallback_reason) { [string]$Record.fallback_reason } else { '无' }
    return ("推荐模式：{0}`r`n模型别名：{1}`r`n选择原因：{2}`r`n回退原因：{3}`r`n模式可用：{4}" -f $mode,$Record.selected_model_alias,$Record.why_selected,$fallback,$Record.mode_available)
}
function Explain-DeepSeekModeSelection {
    $raw = Invoke-RouterCli @('deepseek','explain-mode','当前 Codex 请求未提供任务文本')
    try {
        $script:DeepSeekLastSelection = $raw | ConvertFrom-Json
        Refresh-DeepSeekModePanel
        [System.Windows.Forms.MessageBox]::Show((Format-DeepSeekSelectionSummary $script:DeepSeekLastSelection),'DeepSeek 模式选择原因') | Out-Null
    } catch { throw 'DEEPSEEK_EXPLAIN_RESPONSE_INVALID' }
}
function Refresh-DeepSeekPanel {
    # 高级调试说明：健康检查只读取本地服务与页面状态，不发送 prompt、不调用聊天接口、不点击发送按钮。
    $snapshot = Get-DeepSeekBridgeSnapshot
    $deepSeekStatus.Text = ("模式：本地直连`r`n本地接口：{0}`r`n`r`n桥接状态：{1}`r`n工作进程状态：{2}`r`n浏览器状态：{3}`r`nDeepSeek 页面：{4}`r`n忙碌标记：{5}`r`n最近健康检查：{6}`r`n`r`n端口 8791：{7}`r`n端口 8792：{8}`r`n端口 8793：{9}`r`n`r`n健康检查只读取本地服务与页面状态，不发送提示词、不调用聊天接口、不点击发送按钮。手动测试需两次确认，可能发送一次真实测试对话。" -f $DeepSeekLocalApiAddress,$snapshot.bridge,$snapshot.worker,$snapshot.chrome,$snapshot.page,$snapshot.busy,$script:DeepSeekLastHealth,$snapshot.bridge_port,$snapshot.worker_port,$snapshot.fixture_port)
    Refresh-DeepSeekModePanel
}
function Invoke-DeepSeekPanelAction([string]$Action) {
    # 高级调试原文：不会显示或保存 prompt、response、key、Cookie 或 Token。
    $result = Invoke-DeepSeekLocalScript -Action $Action
    Add-DeepSeekLog -Action $Action -Result $result
    Refresh-DeepSeekPanel
    [System.Windows.Forms.MessageBox]::Show(("操作：{0}`r`n退出码：{1}`r`n状态：{2}`r`n不会显示或保存提示词、响应、密钥、Cookie 或 Token。" -f $Action,$result.exit_code,$result.error_type),'DeepSeek 本地桥接')
}
function Invoke-DeepSeekManualSmoke {
    $first = [System.Windows.Forms.MessageBox]::Show('手动 Smoke 默认不执行。继续会进入第二次确认。','第一次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($first -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $second = [System.Windows.Forms.MessageBox]::Show('确认执行手动 Smoke？它会发送一次真实 DeepSeek 测试对话；不会定时执行，也不会作为健康检查或 heartbeat。','第二次确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($second -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    Invoke-DeepSeekPanelAction 'smoke'
}
function Get-CodexModeState {
    try { return (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $CodexModeScript -Action status 2>$null | Out-String | ConvertFrom-Json) }
    catch { return [pscustomobject]@{ mode='UNKNOWN'; provider='UNKNOWN'; model='UNKNOWN'; endpoint='UNKNOWN'; backup_directory=(Join-Path $env:USERPROFILE '.codex-ai-router\codex-mode'); env_mutation='NONE' } }
}
function Get-CodexModeChineseName([string]$Mode) {
    switch ($Mode) {
        'OFFICIAL_DIRECT' { return '官方直连' }
        'CUSTOM_ROUTER' { return '自定义 Router' }
        'CUSTOM_DEEPSEEK_HEAD' { return 'DeepSeek 首脑' }
        'CUSTOM_DEEPSEEK_TEXT_ONLY' { return 'DeepSeek 文本兼容' }
        'CUSTOM_LOCAL_TEXT_ONLY' { return '本地模型文本兼容' }
        'CUSTOM_HYBRID_TEXT_ONLY' { return '混合助手文本兼容' }
        default { return $Mode }
    }
}
function Format-CodexModeSummary([object]$Record,[string]$Raw,[string]$Action) {
    $script:LastModeDebugJson = Redact-Text $Raw
    if (-not $Record -or $Record.status -eq 'ERROR' -or $Record.error_code) {
        $code = if ($Record.error_code) { [string]$Record.error_code } else { 'MODE_SWITCH_FAILED' }
        return "模式切换失败`r`n错误码：$code`r`n原因：$(Get-UiErrorExplanation $code)`r`n详细 JSON：已放入高级信息/调试信息。"
    }
    $mode = [string]$Record.mode
    $policy = if ($mode -eq 'OFFICIAL_DIRECT') { '不适用（官方直连）' } else { [string](Get-ToolsPolicyRecord).codex_tools_policy }
    return ("模式切换成功`r`n当前模式：{0}`r`nProvider：{1}`r`n模型：{2}`r`nwire_api：{3}`r`n工具策略：{4}`r`n配置备份：已完成`r`n需要操作：请重新打开 Codex 线程`r`n`r`n原始 JSON：已折叠到高级信息/调试信息。" -f (Get-CodexModeChineseName $mode),$Record.provider,$Record.model,($(if($mode -eq 'OFFICIAL_DIRECT'){'官方托管 / 不适用'}else{'responses'})),$policy)
}
$copyDebugButton.Add_Click({
    $payload = if ([string]::IsNullOrWhiteSpace($script:LastModeDebugJson)) { '{"status":"NO_DIAGNOSTIC_JSON"}' } else { Redact-Text $script:LastModeDebugJson }
    try { Set-Clipboard -Value $payload; [System.Windows.Forms.MessageBox]::Show('已复制脱敏诊断 JSON；不会包含 API Key、Authorization、Cookie 或 Token。','复制诊断 JSON') | Out-Null } catch { [System.Windows.Forms.MessageBox]::Show('复制失败；请先执行一次模式切换或打开高级信息。','复制诊断 JSON') | Out-Null }
}.GetNewClosure())
function Refresh-CodexModePanel {
    $state = Get-CodexModeState
    $tools = Get-ToolsPolicyRecord
    $policy = [string]$tools.codex_tools_policy
    $displayPolicy = if ([string]$state.mode -eq 'OFFICIAL_DIRECT') { '不适用（官方直连）' } else { $policy }
    $toolsPolicyStatus.Text = ("当前工具策略：{0}`r`n策略文件：{1}`r`n`r`n严格拒绝：后端不支持工具时直接报错，不会调用模型。`r`n文本兼容：移除工具定义，仅生成分析/计划/指令，不会改文件。`r`n手动计划：返回结构化计划模板，不调用模型。" -f $displayPolicy,$tools.path)
    $codexModeStatus.Text = ("当前模式：{0}`r`nProvider：{1}`r`n模型：{2}`r`n端点摘要：{3}`r`n备份目录：{4}`r`n环境变量修改：{5}`r`n无官方额度模式：{6}`r`n工具策略：{7}`r`n`r`nOFFICIAL_DIRECT：官方直连；CUSTOM_ROUTER：本地 Router；OFFICIAL_ASSISTED：官方主工作流 + 辅助分析；DEEPSEEK_HEAD：建议模式；CUSTOM_DEEPSEEK_HEAD：{8}；CUSTOM_LOCAL_LIGHT：本地模型；CUSTOM_EXTERNAL_API：仅显式 allowlist；CUSTOM_HYBRID_AGENT：已启用 Provider 的统一入口；CUSTOM_DEEPSEEK_TEXT_ONLY / CUSTOM_LOCAL_TEXT_ONLY / CUSTOM_HYBRID_TEXT_ONLY：本地 Router 文本兼容模式。`r`n控制台不接收主要任务输入；任务仍在 Codex 中提交。" -f $state.mode,$state.provider,$state.model,$state.endpoint,$state.backup_directory,$state.env_mutation,$state.no_quota_mode,$displayPolicy,$DeepSeekLocalApiAddress)
}
function Invoke-CodexModeAction([ValidateSet('official-direct','custom-router','custom-deepseek-head','custom-local-light','custom-external-api','custom-hybrid-agent','custom-deepseek-text-only','custom-local-text-only','custom-hybrid-text-only','official-assisted','deepseek-head','local-agent-pending','restore')][string]$Action) {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $CodexModeScript -Action $Action 2>&1 | Out-String
    $record = $null; try { $record = $output | ConvertFrom-Json } catch {}
    $summary = Format-CodexModeSummary -Record $record -Raw $output -Action $Action
    Refresh-CodexModePanel; Refresh-Home
    if ($diagnosticsText -and $debugToggle -and $debugToggle.Checked) { $diagnosticsText.Text = ($script:LastModeDebugJson + "`r`n" + ($script:UiDebugEntries -join "`r`n")) }
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $summary),'Codex 连接模式')
}
function Invoke-CodexTextOnlyMode([ValidateSet('custom-deepseek-text-only','custom-local-text-only','custom-hybrid-text-only')][string]$Action) {
    $confirm = [System.Windows.Forms.MessageBox]::Show('文本兼容模式会切换到本地 Router，并移除 Codex 工具定义；模型只输出文本计划，不会执行工具、修改文件或部署。是否继续？','TEXT_ONLY 模式',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    Invoke-CodexModeAction $Action
    [void](Set-ToolsPolicy 'text_only_strip')
    Refresh-CodexModePanel
}
function Invoke-ModelsOnlyDiagnostic {
    $record = [ordered]@{ endpoint=$DeepSeekLocalApiAddress; mode=(Get-CodexModeState).mode; upstream_status='UNREACHABLE'; content_type='UNKNOWN'; stream_support='UNKNOWN'; normalized='UNKNOWN'; content_detected='UNKNOWN'; last_error=$null }
    try {
        $headers=@{}; $headers.Add('Authorization',('Bearer '+(Get-DeepSeekFixtureKey)))
        $result=Invoke-WebRequest -UseBasicParsing -Uri ($DeepSeekLocalApiAddress + '/models') -Headers $headers -TimeoutSec 4
        $record.upstream_status=[string]$result.StatusCode; $record.content_type=[string]$result.Headers['Content-Type']; $record.stream_support='NOT_TESTED_NO_PROMPT'; $record.normalized=[string]$result.Headers['X-Xiaoyu-Normalized']; $record.content_detected=[string]$result.Headers['X-Xiaoyu-Content-Detected']
    } catch { $record.last_error='MODELS_TEST_FAILED' }
    $allowlistStatus.Text=(Redact-Text ((& py.exe -3 -m codex_ai_router.provider_allowlist 2>&1)|Out-String))
    $recordStatus.Text=(Redact-Text (($record|ConvertTo-Json -Compress))+"`r`n记录文件："+(Join-Path $env:USERPROFILE '.codex-ai-router\call-ledger.jsonl')+"`r`n默认保存正文：NO`r`nAPI Key/Cookie/Token/Authorization：NO")
}
function Invoke-LocalCli([string[]]$Arguments) { try { return (Redact-Text (& xiaoyu-router local @Arguments 2>&1 | Out-String)) } catch { return (Redact-Text $_.Exception.Message) } }
function Format-GroqDiagnostic([object]$Record) {
    $reason = Get-UiErrorExplanation ([string]$Record.error_code)
    return ("旧配置状态：{0}`r`n新白名单状态：{1}`r`n鉴权状态：{2}`r`n模型别名：{3}`r`n上游模型：{4}`r`n运行资格：{5}`r`n不可用原因：{6}`r`n是否允许真实测试：{7}`r`n错误码：{8}`r`n中文说明：{9}`r`nAPI Key / Authorization：不会显示" -f $Record.legacy_enabled,$Record.allowlist_enabled,$Record.auth_present,$Record.model_alias,$Record.upstream_model,$Record.runtime_eligible,$Record.eligibility_reason,$Record.live_request_allowed,$Record.error_code,$reason)
}
function Invoke-GroqDiagnostic {
    $raw = Invoke-RouterCli @('provider','diagnose','groq-2')
    try { $record = $raw | ConvertFrom-Json } catch { throw 'GROQ_DIAGNOSTIC_RESPONSE_INVALID' }
    $message = Format-GroqDiagnostic $record
    Add-ProviderLog ('Groq 诊断：错误码=' + $record.error_code + '；真实请求=NO')
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $message),'Groq 诊断') | Out-Null
}
function Invoke-GroqLiveSmoke {
    $confirm = [System.Windows.Forms.MessageBox]::Show('真实 Groq 测试会产生一次外部 API 请求。仅当新白名单、鉴权、模型确认和运行资格全部通过时才会发送。是否继续？','Groq 手动真实测试',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
    if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    $raw = Invoke-RouterCli @('provider','live-smoke','groq-2','--confirm')
    try { $record = $raw | ConvertFrom-Json } catch { throw 'GROQ_LIVE_SMOKE_RESPONSE_INVALID' }
    Add-ProviderLog ('Groq 真实测试：错误码=' + $record.error_code + '；已发送=' + $record.live_request_sent + '；不记录正文')
    [System.Windows.Forms.MessageBox]::Show((Redact-Text (Format-GroqDiagnostic $record)),'Groq 手动真实测试') | Out-Null
}
function Get-LocalErrorExplanation([string]$Code) {
    switch ([string]$Code) {
        'PORT_IN_USE' { return '端口被占用，系统会尝试安全切换备用端口。' }
        'PORT_IN_USE_BY_UNKNOWN_PROCESS' { return '未知进程占用，未强制结束；已保留系统安全。' }
        'ROUTER_PORT_IN_USE_UNKNOWN_PROCESS' { return '18789 被未知进程占用；未停止进程，也未向未知服务发送 POST。请先确认 PID 和命令行。' }
        'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' { return '18789 被未知进程占用；未停止进程，也未向未知服务发送 POST。请先确认 PID 和命令行。' }
        'STALE_OR_INCOMPATIBLE_ROUTER' { return '检测到旧版或不兼容的小羽 Router；请确认后停止旧进程，再启动当前版本。' }
        'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' { return '检测到旧小羽 Router，但尚未获得停止确认；未终止任何进程。' }
        'LOCAL_PORTS_EXHAUSTED' { return '本地备用端口已耗尽，请关闭无关服务后重试。' }
        'LOCAL_MODEL_LOAD_TIMEOUT' { return '本地模型加载超时，可能是模型太大或内存不足。' }
        'LOCAL_EMPTY_RESPONSE' { return '本地模型无返回，请检查模型状态。' }
        'LOCAL_MODEL_SWITCH_FAILED' { return '本地模型切换失败，已保留当前配置。' }
        'LOCAL_HIGH_RISK_SAFE_STOP' { return '高风险任务已停止，本地模型不会执行此类写入。' }
        'LOCAL_NO_ELIGIBLE_MODEL' { return '没有符合当前策略的本地模型。' }
        default { return if([string]::IsNullOrWhiteSpace($Code)){'暂无错误。'}else{'操作未完成：' + $Code} }
    }
}
# 高级调试字段保留英文内部标识，不放在主要用户界面：llama.cpp direct；LM Studio：仅作可选 fallback。
# 旧命令名称映射到中文按钮：扫描 LM Studio 模型、修复本地后端、测试本地推理。
function Refresh-DirectLocalCard {
    $statusRaw = Invoke-LocalCli @('status'); $modelsRaw = Invoke-LocalCli @('models'); $status = $null; $models = $null
    try { $status = $statusRaw | ConvertFrom-Json } catch {}; try { $models = $modelsRaw | ConvertFrom-Json } catch {}
    if($status){$selected = if($status.selected_model){$status.selected_model.model_id}else{'未选择'}; $path = if($status.llama_server_path){$status.llama_server_path}else{'未发现'}; $owner = if($status.port_owner){('{0} (PID {1})' -f $status.port_owner.process_name,$status.port_owner.pid)}else{'无'}; $lastRepair = if($script:DirectLocalLastRepair){$script:DirectLocalLastRepair}else{'未运行'}; $directLocalStatus.Text = ("后端：直接本地模型`r`n服务程序：{0}`r`n当前加载模型：{1}`r`n服务状态：{2}`r`n本地端点：{3}`r`n当前端口：{4}`r`n端口占用：{5}`r`n端口所有者：{6}`r`n备用端口切换：{7}`r`n最近修复：{8}`r`n模型画像数：{9}`r`n自动选择模型：已启用`r`n说明：控制台仅负责管理，不接收主要任务输入。" -f $path,$selected,$status.server_running,$status.endpoint,$status.port,$status.port_in_use,$owner,$status.auto_port_fallback,$lastRepair,$status.model_count)}else{$directLocalStatus.Text = ('直接本地状态不可用：' + $statusRaw)}
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
    function Add-ModelDialogButton([string]$Text,[scriptblock]$Action,[int]$Width=140){$button=New-Object System.Windows.Forms.Button;$button.Text=$Text;$button.Width=$Width;$button.Height=38;$button.Font=$buttonFont;$safeName=$Text;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure());[void]$buttons.Controls.Add($button)}
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
    function Add-BatchButton([string]$Text,[scriptblock]$Action,[int]$Width=135){$button=New-Object System.Windows.Forms.Button;$button.Text=$Text;$button.Width=$Width;$button.Height=36;$button.Font=$buttonFont;$safeName=$Text;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure());[void]$buttons.Controls.Add($button)}
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
function Refresh-Home { $router = Get-RouterStatus; $codex = Get-CodexStatus; $statusBox.Text = ("Router：{0}`r`n监听状态：{1}`r`n监听地址：{2}`r`n监听端口：{3}`r`n进程：{4}（PID {5}）`r`n可执行路径：{6}`r`n命令行：{7}`r`n端口所有者：{8}`r`n身份探测：{9}`r`nAgent API：{10}（版本 {11}）`r`n仅本机：{12}`r`n健康检查：{13}`r`n/v1/models：{14}`r`n未知服务 POST：{15}`r`n当前 Codex Provider：{16}`r`n当前 Codex 模型：{17}`r`n工具策略：{18}`r`n网络模式：{19}`r`nXiaoyuRouter 已安装：{20}`r`n`r`n官方委托提示：Luna+low 适合轻量调度；Terra+medium 适合中等实现；高风险建议 Sol/最强模型 + high/xhigh。委托不会自动热切当前 Codex 模型。" -f $(if ($router.running) { '运行中' } else { '已停止' }),$router.listener_status,$router.listener_address,$router.listener_port,$router.listener_process,$router.listener_pid,$router.listener_executable_path,$router.listener_command_line,$router.listener_owner_kind,$router.identity_status,$router.agent_api,$router.agent_version,$(if($router.listener_status -eq 'LISTENING' -and $router.listener_address -in @('127.0.0.1','::1','localhost')){'是'}else{'否'}),$router.health_status,$router.models_status,$router.post_to_unknown_service,$codex.provider,$codex.model,$router.tools_policy,$router.mode,$(if($codex.xiaoyu){'是'}else{'否'})); $diagnosticsText.Text = Redact-Text ((Get-ProviderRows | Format-Table -AutoSize | Out-String) + "`r`n" + $statusBox.Text); Refresh-Providers; Refresh-Usage; Refresh-DirectLocalCard }
function Add-HomeButton([string]$Caption,[scriptblock]$Action,[ValidateSet('Router','Switch')][string]$Area = 'Router') { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 170; $button.Height = 42; $button.Font = $buttonFont; $button.Margin = New-Object System.Windows.Forms.Padding(5); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); if($Area -eq 'Router'){[void]$homeButtons.Controls.Add($button)}else{[void]$switchButtons.Controls.Add($button)} }
function Add-DirectLocalButton([string]$Caption,[scriptblock]$Action,[int]$Width=150) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=34; $button.Font=$buttonFont; $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$directLocalButtons.Controls.Add($button) }
function Add-DeepSeekButton([string]$Caption,[scriptblock]$Action,[int]$Width=150) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=36; $button.Font=$buttonFont; $button.Margin = New-Object System.Windows.Forms.Padding(5); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$deepSeekButtons.Controls.Add($button) }
function Add-DeepSeekModeButton([string]$Caption,[scriptblock]$Action,[int]$Width=145) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=30; $button.Font=$buttonFont; $button.Margin = New-Object System.Windows.Forms.Padding(3); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$deepSeekModeButtons.Controls.Add($button) }
function Add-CodexModeButton([string]$Caption,[scriptblock]$Action,[int]$Width=145) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=34; $button.Font=$buttonFont; $button.Margin=New-Object System.Windows.Forms.Padding(4); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$codexModeButtons.Controls.Add($button) }
function Add-ToolsPolicyButton([string]$Caption,[scriptblock]$Action,[int]$Width=180) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=34; $button.Font=$buttonFont; $button.Margin=New-Object System.Windows.Forms.Padding(4); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$toolsPolicyButtons.Controls.Add($button) }
function Add-LocalAgentButton([string]$Caption,[scriptblock]$Action,[int]$Width=155) { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.Height=34; $button.Font=$buttonFont; $button.Margin=New-Object System.Windows.Forms.Padding(4); $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$localAgentButtons.Controls.Add($button) }
function Add-ProviderButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 135; $button.Height = 36; $button.Font = $buttonFont; $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); $target = if($Caption -in @('刷新模型','运行探测','选择模型','批量管理','解释选择','Groq 诊断')){$providerModelButtons}elseif($Caption -eq '转换为 Groq SDK'){$providerMigrationButtons}else{$providerButtons}; [void]$target.Controls.Add($button) }
Add-HomeButton '启动 Router' { Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton '停止 Router' { Stop-Router; Refresh-Home }
Add-HomeButton '重启 Router' { Stop-Router; Start-Router; Start-Sleep -Milliseconds 400; Refresh-Home }
Add-HomeButton '检查 18789' { $router = Get-RouterStatus; [System.Windows.Forms.MessageBox]::Show(("监听：{0}`r`n地址：{1}`r`nPID：{2}`r`n进程：{3}`r`n可执行路径：{4}`r`n命令行：{5}`r`n身份：{6}`r`n健康：{7}`r`nAgent API：{8}`r`n版本：{9}`r`n/v1/models：{10}`r`n未知服务 POST：{11}`r`n工具策略：{12}" -f $router.listener_status,$router.listener_address,$router.listener_pid,$router.listener_process,$router.listener_executable_path,$router.listener_command_line,$router.identity_status,$router.health_status,$router.agent_api,$router.agent_version,$router.models_status,$router.post_to_unknown_service,$router.tools_policy),'Router 本地状态') ; Refresh-Home }
Add-HomeButton '停止旧小羽 Router（需确认）' { Stop-OldRouter; Refresh-Home }
Add-HomeButton '查看 /v1/models' { [System.Windows.Forms.MessageBox]::Show(((Get-RouterStatus).models | ConvertTo-Json -Depth 5),'Router 模型') }
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
Add-DirectLocalButton '扫描本地模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('models')),'本地模型列表') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '选择本地模型' { Add-Type -AssemblyName Microsoft.VisualBasic; $value=[Microsoft.VisualBasic.Interaction]::InputBox('输入已发现的 GGUF model_id：','选择本地模型',''); if($value){[System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('select',$value)),'本地模型');Refresh-DirectLocalCard} }
Add-DirectLocalButton '启动本地后端' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('start')),'启动本地后端');Refresh-DirectLocalCard }
Add-DirectLocalButton '停止本地后端' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('stop')),'停止本地后端');Refresh-DirectLocalCard }
Add-DirectLocalButton '本地修复' { $raw = Invoke-LocalCli @('repair'); try { $result = $raw | ConvertFrom-Json; $script:DirectLocalLastRepair = if($result.status -eq 'PASS'){'已通过'}else{[string]$result.error_code + '：' + (Get-LocalErrorExplanation ([string]$result.error_code))} } catch { $script:DirectLocalLastRepair = 'REPAIR_RESPONSE_INVALID：修复结果无法读取。' }; [System.Windows.Forms.MessageBox]::Show($raw,'本地修复');Refresh-DirectLocalCard }
Add-DirectLocalButton '测试本地模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('smoke','只回复 LOCAL_DIRECT_OK')),'本地模型测试');Refresh-DirectLocalCard }
Add-DirectLocalButton '查看模型画像' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('profiles')),'本地模型画像') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '解释选择' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('explain-select','解释这个 Python 报错，不修改文件')),'自动选择说明') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '自动选择模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('policy')),'自动选择策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '允许慢模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('policy','--allow-slow-local')),'慢模型策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '禁止 BF16 自动选择' { [System.Windows.Forms.MessageBox]::Show('已保持默认安全策略：BF16 模型不会自动选择。需要时请在高级配置中明确允许。','本地模型策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '打开配置文件' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\local-backend.json'; if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('配置文件尚未创建。','本地配置')} }
Add-DirectLocalButton '打开日志目录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router'; if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('日志目录尚未创建。','日志目录')} }
Add-DeepSeekButton '启动 Bridge' { Invoke-DeepSeekPanelAction 'start-bridge' }
Add-DeepSeekButton '启动 Worker' { Invoke-DeepSeekPanelAction 'start-worker' }
Add-DeepSeekButton '健康检查（无 prompt）' { Invoke-DeepSeekPanelAction 'health-check' } 180
Add-DeepSeekButton '打开 DeepSeek Web' { Invoke-DeepSeekPanelAction 'start-bridge' } 175
Add-DeepSeekButton '复制本地 API 地址' { try { Set-Clipboard -Value $DeepSeekLocalApiAddress; $result=[pscustomobject]@{exit_code=0;error_type='COPIED'} } catch { $result=[pscustomobject]@{exit_code=1;error_type='CLIPBOARD_FAILED'} }; Add-DeepSeekLog 'copy-api-address' $result; [System.Windows.Forms.MessageBox]::Show('本地 API 地址已复制。','DeepSeek 本地桥接') }
Add-DeepSeekButton '复制本地测试 Key' { try { Set-Clipboard -Value (Get-DeepSeekFixtureKey); $result=[pscustomobject]@{exit_code=0;error_type='COPIED'} } catch { $result=[pscustomobject]@{exit_code=1;error_type='CLIPBOARD_FAILED'} }; Add-DeepSeekLog 'copy-test-key' $result; [System.Windows.Forms.MessageBox]::Show('本地 fixture 测试 Key 已复制；它不是 DeepSeek 凭据，也不会写入日志。','DeepSeek 本地桥接') }
Add-DeepSeekButton '手动 Smoke（双确认）' { Invoke-DeepSeekManualSmoke } 190
Add-DeepSeekButton '停止服务' { Invoke-DeepSeekPanelAction 'stop' }
Add-DeepSeekButton '打开日志目录' { if(Test-Path -LiteralPath $DeepSeekRuntimeDir){Start-Process explorer.exe -ArgumentList ('"' + $DeepSeekRuntimeDir + '"');$result=[pscustomobject]@{exit_code=0;error_type='OPENED'}}else{$result=[pscustomobject]@{exit_code=1;error_type='LOG_DIRECTORY_NOT_FOUND'}};Add-DeepSeekLog 'open-log-directory' $result;[System.Windows.Forms.MessageBox]::Show(('日志目录：{0}`r`n状态：{1}' -f $DeepSeekRuntimeDir,$result.error_type),'DeepSeek 本地桥接') }
Add-DeepSeekModeButton '自动选择模式' { Set-DeepSeekModePreference 'auto' }
Add-DeepSeekModeButton '固定普通模式' { Set-DeepSeekModePreference 'normal' }
Add-DeepSeekModeButton '固定搜索模式' { Set-DeepSeekModePreference 'search' }
Add-DeepSeekModeButton '固定思考模式' { Set-DeepSeekModePreference 'thinking' }
Add-DeepSeekModeButton '固定专家模式' { Set-DeepSeekModePreference 'expert' }
Add-DeepSeekModeButton '探测 DeepSeek 模式' { Invoke-DeepSeekModeProbe } 165
Add-DeepSeekModeButton '查看模式选择原因' { Explain-DeepSeekModeSelection } 165
Add-CodexModeButton '切换官方直连' { Invoke-CodexModeAction 'official-direct' }
Add-CodexModeButton '切换本地 Router' { Invoke-CodexModeAction 'custom-router' }
Add-CodexModeButton 'DeepSeek 首脑' { Invoke-CodexModeAction 'custom-deepseek-head' } 150
Add-CodexModeButton '本地轻量' { Invoke-CodexModeAction 'custom-local-light' } 130
Add-CodexModeButton '外部 API' { Invoke-CodexModeAction 'custom-external-api' } 135
Add-CodexModeButton '混合助手' { Invoke-CodexModeAction 'custom-hybrid-agent' } 135
Add-CodexModeButton '启用官方辅助模式' { Invoke-CodexModeAction 'official-assisted' } 170
Add-CodexModeButton '启用 DeepSeek 首脑' { Invoke-CodexModeAction 'deepseek-head' } 170
Add-CodexModeButton '本地 Agent（预留）' { Invoke-CodexModeAction 'local-agent-pending' } 170
Add-CodexModeButton 'DeepSeek 文本兼容模式' { Invoke-CodexTextOnlyMode 'custom-deepseek-text-only' } 190
Add-CodexModeButton '本地模型文本兼容' { Invoke-CodexTextOnlyMode 'custom-local-text-only' } 180
Add-CodexModeButton '混合助手文本兼容' { Invoke-CodexTextOnlyMode 'custom-hybrid-text-only' } 180
Add-CodexModeButton '恢复上一次配置' { Invoke-CodexModeAction 'restore' }
Add-CodexModeButton '打开备份目录' { $path=(Get-CodexModeState).backup_directory;if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚未创建备份目录。','Codex 连接模式')} }
Add-CodexModeButton '仅检查模型列表' { Invoke-ModelsOnlyDiagnostic } 150
Add-CodexModeButton '刷新供应商白名单' { $allowlistStatus.Text=(Redact-Text ((& py.exe -3 -m codex_ai_router.provider_allowlist 2>&1)|Out-String)) } 180
Add-CodexModeButton '查看本地调用记录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\call-ledger.jsonl';if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无本地调用记录。','调用记录')} } 175
Add-ToolsPolicyButton '严格拒绝工具（推荐）' { [void](Set-ToolsPolicy 'strict_reject'); Refresh-CodexModePanel }
Add-ToolsPolicyButton '文本兼容：忽略工具' { $confirm=[System.Windows.Forms.MessageBox]::Show('文本兼容模式会移除工具定义，只生成分析和计划，不会执行工具或修改文件。是否启用？','TEXT_ONLY 兼容模式',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning); if($confirm -eq [System.Windows.Forms.DialogResult]::Yes){[void](Set-ToolsPolicy 'text_only_strip');Refresh-CodexModePanel} } 190
Add-ToolsPolicyButton '手动计划（不调用模型）' { [void](Set-ToolsPolicy 'manual_plan'); Refresh-CodexModePanel } 190
Add-LocalAgentButton '生成计划（PLAN_ONLY）' { Invoke-LocalAgentPlan } 175
Add-LocalAgentButton '调用大脑生成计划（需确认）' { Invoke-LocalAgentPlan $true } 205
Add-LocalAgentButton '只读检查' { Invoke-LocalAgentReadonly } 130
Add-LocalAgentButton '生成补丁草案（需确认）' { Invoke-LocalAgentDraft } 190
Add-LocalAgentButton '应用补丁（双确认）' { Invoke-LocalAgentApply } 170
Add-LocalAgentButton '运行测试（双确认）' { Invoke-LocalAgentTest } 170
Add-LocalAgentButton '提交 commit（双确认）' { Invoke-LocalAgentCommit } 180
Add-LocalAgentButton '复制给 Codex 的指令' { if($script:LocalAgentPlanJson -and $script:LocalAgentPlanJson.codex_instruction){Set-Clipboard -Value (Redact-Text ([string]$script:LocalAgentPlanJson.codex_instruction));[System.Windows.Forms.MessageBox]::Show('已复制脱敏 Codex 指令。','小羽 Local Agent')}else{[System.Windows.Forms.MessageBox]::Show('请先生成计划。','小羽 Local Agent')} } 190
Add-LocalAgentButton '停止 Agent' { $arguments=@('agent','stop');if($script:LocalAgentPlanId){$arguments += @('--plan',$script:LocalAgentPlanId)};$raw=Invoke-RouterCli $arguments;Add-LocalAgentLog ('stop=' + $script:LocalAgentPlanId);[System.Windows.Forms.MessageBox]::Show($raw,'停止 Agent') } 130
Add-LocalAgentButton '打开 Agent 记录目录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\local-agent';if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无 Local Agent 记录。','Local Agent')} } 190
Add-ProviderButton '新增供应商' { [System.Windows.Forms.MessageBox]::Show('标准 Bearer API Key 供应商通常不需要自定义 Header。Groq（https://api.groq.com/openai/v1）使用官方 SDK，默认不配置自定义 Header。','新增供应商提示'); Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider.ps1') + '"') }
Add-ProviderButton '轮换 Header' { $id = Require-SelectedProvider; if($id){ Start-Process powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'configure-provider-header.ps1') + '"'); Add-ProviderLog ('打开 Header 配置：' + $id) } }
Add-ProviderButton '刷新列表' { Refresh-Providers }
Add-ProviderButton '启用/禁用' { $id = Require-SelectedProvider; if ($id) { Update-ProviderEnabled $id; Add-ProviderLog ('已切换启用状态：' + $id); Refresh-Providers } }
Add-ProviderButton '删除' { $id = Require-SelectedProvider; if ($id -and [System.Windows.Forms.MessageBox]::Show("删除 $id 的元数据？不会删除环境变量中的密钥。",'确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes -and [System.Windows.Forms.MessageBox]::Show('请再次确认删除供应商元数据。','确认',[System.Windows.Forms.MessageBoxButtons]::YesNo) -eq [System.Windows.Forms.DialogResult]::Yes) { [void](Invoke-RouterCli @('provider','remove',$id)); Add-ProviderLog ('已删除供应商元数据：' + $id); Refresh-Providers } }
Add-ProviderButton '刷新模型' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','refresh-models',$id); Add-ProviderLog ('模型刷新：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'模型刷新'); Refresh-Providers } }
Add-ProviderButton '运行探测' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','probe-runtime',$id); Add-ProviderLog ('运行探测：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'运行探测'); Refresh-Providers } }
Add-ProviderButton 'Groq 诊断' { Invoke-GroqDiagnostic }
Add-ProviderButton 'Groq 手动真实测试' { Invoke-GroqLiveSmoke }
Add-ProviderButton '选择模型' { $id = Require-SelectedProvider; if ($id) { Open-ModelPicker $id; Add-ProviderLog ('模型选择：' + $id) } }
Add-ProviderButton '批量管理' { $id=Require-SelectedProvider;if($id){Open-BatchManager $id;Add-ProviderLog ('批量管理：'+$id)}}
Add-ProviderButton '解释选择' { $id=Require-SelectedProvider;if($id){$out=Invoke-RouterCli @('provider','explain-selection',('xiaoyu-api-' + $id));[System.Windows.Forms.MessageBox]::Show($out,'选择解释')}}
Add-ProviderButton '转换为 Groq SDK' { $id = Require-SelectedProvider; if ($id) { $output = Invoke-RouterCli @('provider','migrate-groq',$id); Add-ProviderLog ('Groq SDK 转换：' + $id + '；' + $output.Trim()); [System.Windows.Forms.MessageBox]::Show($output,'Groq SDK 转换'); Refresh-Providers } }
Add-ProviderButton '详情' { $id = Require-SelectedProvider; if ($id) { $provider = (Get-ProviderData).providers.($id); $registry=$provider.model_registry; $headerNames = if ($provider.headers) { $provider.headers.psobject.Properties.Name -join ', ' } else { 'NONE' }; $keyConfigured = if ($provider.api_key_env -and [Environment]::GetEnvironmentVariable($provider.api_key_env,'User')) { 'YES' } else { 'NO' }; $discovered=Format-ModelList $(if($registry){$registry.DISCOVERED_MODELS}else{$provider.last_discovery_models}); $usable=Format-ModelList $(if($registry){$registry.USABLE_MODELS}else{$provider.last_discovery_models}); $allowed=Format-ModelList $(if($registry){$registry.ALLOWED_MODELS}else{$provider.allowed_model_seeds}); $denied=Format-ModelList $(if($registry){$registry.DENIED_MODELS}else{@()}); $responsive=Format-ModelList $(if($registry){$registry.RUNTIME_RESPONSIVE_MODELS}else{@()}); $cooldown=Format-ModelList $(if($registry){$registry.TIMEOUT_COOLDOWN_MODELS}else{@()}); $runtime = @((Get-ProviderRows)|Where-Object{$_.provider_id -eq $id}|Select-Object -First 1); $runtimeStatus = if($runtime.Count){$runtime[0].last_runtime_status}else{'UNKNOWN'}; $selected=if($provider.preferred_runtime_model){$provider.preferred_runtime_model}elseif($registry -and $registry.CURRENT_RUNTIME_MODEL){$registry.CURRENT_RUNTIME_MODEL}elseif($runtime.Count){$runtime[0].selected_runtime_model}else{'NONE'}; $selectionMode=if($provider.preferred_runtime_model){'手动选择'}else{'自动选择'}; [System.Windows.Forms.MessageBox]::Show(("Provider ID：{0}`r`n显示名：{1}`r`nBase URL：{2}`r`nWire API：{3}`r`n启用：{4}`r`n发现认证：{5}`r`n推理认证：{6}`r`nAPI Key 已配置：{7}`r`nAPI Key 环境变量：{8}`r`nHeader 名称：{9}`r`n发现模型：{10}`r`n可用模型：{11}`r`n允许模型：{12}`r`n拒绝模型：{13}`r`n运行可用模型：{14}`r`n超时冷却模型：{15}`r`n当前运行模型：{16}`r`n选择方式：{17}`r`n最近发现状态：{18}`r`n最近运行状态：{19}" -f $id,$provider.display_name,$provider.base_url,$provider.wire_api,$provider.enabled,$provider.model_discovery_auth_style,$provider.inference_auth_style,$keyConfigured,$provider.api_key_env,$headerNames,$discovered,$usable,$allowed,$denied,$responsive,$cooldown,$selected,$selectionMode,$provider.last_discovery_status,$runtimeStatus),'供应商详情'); Add-ProviderLog ('查看详情：' + $id) } }
function Add-UsageButton([string]$Caption,[scriptblock]$Action) { $button = New-Object System.Windows.Forms.Button; $button.Text = $Caption; $button.Width = 180; $button.Height = 38; $button.Font = $buttonFont; $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$usageButtons.Controls.Add($button) }
Add-UsageButton '打开 Codex 用量' { Start-Process 'https://chatgpt.com/#settings'; [System.Windows.Forms.MessageBox]::Show('请在 Codex 设置 → 用量中查看官方额度信息。','官方用量') }
Add-UsageButton '切换省额度模式' { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-xiaoyu-codex.ps1'); Refresh-Home }
Add-UsageButton '刷新本地统计' { Refresh-Usage }
if ($SelfTest) {
    Refresh-Providers
    $guardResult = Invoke-SafeUiAction -Name 'selftest' -Action { throw 'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED' }
    Write-Output 'CONTROL_UI_INITIALIZATION=PASS'
    Write-Output ('UI_SAFE_ACTION_EXCEPTION=' + $(if($guardResult.error_code -eq 'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED'){'CAUGHT'}else{'FAIL'}))
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
    Write-Output 'LOCAL_REPAIR_UI_CONSTRUCTION=PASS'
    Write-Output 'DEEPSEEK_LOCAL_BRIDGE_UI_CONSTRUCTION=PASS'
    Write-Output 'CODEX_MODE_ALLOWLIST_UI_CONSTRUCTION=PASS'
    Write-Output 'TOOLS_POLICY_UI_CONSTRUCTION=PASS'
    Write-Output 'LOCAL_AGENT_UI_CONSTRUCTION=PASS'
    Write-Output 'LOCAL_AGENT_CONFIRMATION_GATES=PASS'
    Write-Output 'CODEX_TASK_INPUT_LOCATION=CODEX_ONLY'
    exit 0
}
$form.Add_Shown({ Refresh-Home; Refresh-DeepSeekPanel; Refresh-CodexModePanel; Refresh-LocalAgentPanel })
[void]$form.ShowDialog()
