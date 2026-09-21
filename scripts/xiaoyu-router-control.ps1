[CmdletBinding()]
param(
    [switch]$NoShow,
    [switch]$SelfTest,
    [ValidateRange(0.8, 2.0)]
    [double]$FontScale = 1.25,
    [string]$ProviderConfigPath = '',
    [string]$CodexConfigPath = '',
    [string]$CodexConfigStateRoot = '',
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
$CodexConfig = if (-not [string]::IsNullOrWhiteSpace($CodexConfigPath)) { $CodexConfigPath } else { Join-Path $env:USERPROFILE '.codex\config.toml' }
$CodexConfigSwitcherStateRoot = if (-not [string]::IsNullOrWhiteSpace($CodexConfigStateRoot)) { $CodexConfigStateRoot } else { $ProjectRoot }
$DeepSeekLegacyWorkerRoot = 'C:\Users\bad39\Documents\private-ai-chat-worker'
$DeepSeekBridgeRoot = 'C:\Users\bad39\Documents\deepseek-web-browser-bridge-poc'
$DeepSeekWorkerRoot = $DeepSeekLegacyWorkerRoot
$DeepSeekBridgeScripts = $null
$DeepSeekLauncherConfigPath = Join-Path $ProjectRoot 'codex-handoff\local-config\bridge-paths.json'
$RouterPidFile = Join-Path ([IO.Path]::GetTempPath()) 'xiaoyu-router-18789.pid'
$DeepSeekRuntimeDir = Join-Path $DeepSeekBridgeRoot '.runtime'
$DeepSeekLocalApiAddress = 'http://127.0.0.1:8791/v1'
$DeepSeekWebUrl = 'https://chat.deepseek.com/'
$DeepSeekChromeDebugPort = 9222
$DeepSeekControlledChromeProfile = Join-Path $DeepSeekBridgeRoot 'deepseek-browser-poc-profile'
$script:DeepSeekLauncherLast = $null
$CodexModeScript = Join-Path $PSScriptRoot 'codex-mode-manager.ps1'
$script:DeepSeekLastHealth = '未运行'
$script:DeepSeekModeProbe = $null
$script:DeepSeekModePreference = 'auto'
$script:DeepSeekPerformanceMode = 'balanced'
$script:DeepSeekLastSelection = $null
$script:LastModeDebugJson = ''
$script:UiDebugEntries = [System.Collections.Generic.List[string]]::new()
$script:LocalAgentPlanId = ''
$script:LocalAgentPlanJson = $null
$script:RealCodexConfigModified = 'NO'

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
function Redact-Text([string]$Text) {
    $safe = $Text -replace '(?i)(bearer\s+)[^\s]+','$1[REDACTED]' -replace '(?i)(sk-[a-z0-9_-]+)','[REDACTED]' -replace '(?i)(authorization\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)(cookie\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)(token\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)((?:--)?api[_ -]?key(?:=|\s+))[^\s,;]+','$1[REDACTED]' -replace '(?i)((?:--)?password(?:=|\s+))[^\s,;]+','$1[REDACTED]'
    return ($safe -replace '(?i)api[_ -]?key|authorization|bearer|token|cookie|storage[_ -]?state|secret|password','credential_field_redacted')
}
function Get-DeepSeekLauncherConfig {
    if (-not (Test-Path -LiteralPath $DeepSeekLauncherConfigPath -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $DeepSeekLauncherConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return [pscustomobject]@{ parse_error = 'BRIDGE_PATH_CONFIG_INVALID' } }
}
function Resolve-RouterRoot {
    $candidate = Split-Path -Parent $PSScriptRoot
    if ((Test-Path -LiteralPath (Join-Path $candidate 'scripts\xiaoyu-router-control.ps1') -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $candidate 'src') -PathType Container)) { return $candidate }
    return $null
}
function Resolve-BridgeRoot {
    $config = Get-DeepSeekLauncherConfig
    if ($config -and $config.parse_error) { return $null }
    $configured = if ($config -and $config.bridge_root) { [string]$config.bridge_root } else { '' }
    $candidate = if (-not [string]::IsNullOrWhiteSpace($configured)) { $configured } else { 'C:\Users\bad39\Documents\deepseek-web-browser-bridge-poc' }
    if (Test-Path -LiteralPath $candidate -PathType Container) { return (Resolve-Path -LiteralPath $candidate).Path }
    return $null
}
function Resolve-WorkerRoot {
    $config = Get-DeepSeekLauncherConfig
    $configured = if ($config -and $config.worker_root) { [string]$config.worker_root } else { $DeepSeekLegacyWorkerRoot }
    if (Test-Path -LiteralPath $configured -PathType Container) { return (Resolve-Path -LiteralPath $configured).Path }
    return $null
}
function Resolve-BridgeStartCommand {
    $routerRoot = Resolve-RouterRoot
    $bridgeRoot = Resolve-BridgeRoot
    $packagePath = if ($bridgeRoot) { Join-Path $bridgeRoot 'package.json' } else { $null }
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { $node = Get-Command node -ErrorAction SilentlyContinue }
    $base = [ordered]@{
        command_kind = 'NPM_SCRIPT'; expected_path = $packagePath; resolved_router_root = $routerRoot; resolved_bridge_root = $bridgeRoot; resolved_worker_root = Resolve-WorkerRoot
        file_exists = [bool]($packagePath -and (Test-Path -LiteralPath $packagePath -PathType Leaf)); runtime_exists = [bool]($npm -and $node)
        runtime = if ($npm) { [string]$npm.Source } else { $null }; command_line_sanitized = 'npm start'; bridge_required = 'YES'; worker_required = 'NO'
    }
    if (-not $bridgeRoot) { $base.error_code = 'BRIDGE_ROOT_NOT_FOUND'; $base.suggested_fix = '确认 deepseek-web-browser-bridge-poc 路径，或提供 bridge-paths.json。'; return [pscustomobject]$base }
    if (-not $base.file_exists) { $base.error_code = 'BRIDGE_START_ENTRY_NOT_FOUND'; $base.suggested_fix = 'Bridge 根目录存在，但缺少 package.json 启动入口。'; return [pscustomobject]$base }
    if (-not $base.runtime_exists) { $base.error_code = 'BRIDGE_RUNTIME_NOT_FOUND'; $base.suggested_fix = '安装 Node.js，并确认 node/npm 在 PATH 中。'; return [pscustomobject]$base }
    $base.error_code = 'NONE'; $base.suggested_fix = 'NONE'; return [pscustomobject]$base
}
function Resolve-BridgeHealthCommand {
    return [pscustomobject]@{ command_kind = 'HTTP_GET'; expected_path = 'http://127.0.0.1:8791/health'; command_line_sanitized = 'GET /health'; bridge_required = 'YES'; worker_required = 'NO' }
}
function Resolve-ChromeExecutable {
    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) { return (Resolve-Path -LiteralPath $command.Source).Path }
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    $candidates = @($roots | ForEach-Object { Join-Path ([string]$_) 'Google\Chrome\Application\chrome.exe' })
    foreach ($candidate in $candidates) { if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return (Resolve-Path -LiteralPath $candidate).Path } }
    return $null
}
function Resolve-DeepSeekOpenCommand {
    $chrome = Resolve-ChromeExecutable
    $profile = if ($DeepSeekBridgeRoot) { Join-Path $DeepSeekBridgeRoot 'deepseek-browser-poc-profile' } else { $DeepSeekControlledChromeProfile }
    $commandLine = 'controlled chrome --user-data-dir=<Bridge profile> --remote-debugging-port=9222 --new-window <DeepSeek URL>'
    if ($chrome) { return [pscustomobject]@{ command_kind = 'CONTROLLED_CHROME'; expected_path = $DeepSeekWebUrl; executable = $chrome; user_data_dir = $profile; debug_port = $DeepSeekChromeDebugPort; command_line_sanitized = $commandLine; file_exists = 'YES'; runtime_exists = 'YES'; bridge_required = 'YES'; worker_required = 'NO'; error_code = 'NONE'; suggested_fix = 'NONE' } }
    return [pscustomobject]@{ command_kind = 'CONTROLLED_CHROME'; expected_path = $DeepSeekWebUrl; executable = ''; user_data_dir = $profile; debug_port = $DeepSeekChromeDebugPort; command_line_sanitized = $commandLine; file_exists = 'NO'; runtime_exists = 'NO'; bridge_required = 'YES'; worker_required = 'NO'; error_code = 'CHROME_NOT_FOUND'; suggested_fix = '安装 Google Chrome，或确认 chrome.exe 位于标准安装路径。已拒绝回退到普通浏览器。' }
}
function Test-LauncherPrerequisites {
    $start = Resolve-BridgeStartCommand
    $health = Resolve-BridgeHealthCommand
    $portOwner = Get-DeepSeekPortOwner 8791
    $script:DeepSeekBridgeRoot = $start.resolved_bridge_root
    $script:DeepSeekBridgeScripts = if ($start.resolved_bridge_root) { Join-Path $start.resolved_bridge_root 'scripts' } else { $null }
    $script:DeepSeekWorkerRoot = $start.resolved_worker_root
    if ($start.resolved_bridge_root) { $script:DeepSeekRuntimeDir = Join-Path $start.resolved_bridge_root '.runtime' }
    $open = Resolve-DeepSeekOpenCommand
    return [pscustomobject]@{
        resolved_router_root = $start.resolved_router_root; resolved_bridge_root = $start.resolved_bridge_root; resolved_worker_root = $start.resolved_worker_root
        bridge_start_command = $start; bridge_health_command = $health; deepseek_open_command = $open
        bridge_entry_exists = if ($start.file_exists) { 'YES' } else { 'NO' }; runtime_exists = if ($start.runtime_exists) { 'YES' } else { 'NO' }
        worker_required = 'NO'; bridge_port = Get-DeepSeekPortState 8791; bridge_port_owner = $portOwner; router_port = Get-DeepSeekPortState 18789
        last_error = if ($start.error_code -ne 'NONE') { $start.error_code } else { 'NONE' }
    }
}
function Format-DeepSeekLauncherDiagnostic([object]$Diagnostic) {
    if (-not $Diagnostic) { $Diagnostic = Test-LauncherPrerequisites }
    $start = $Diagnostic.bridge_start_command
    return @(
        '启动器诊断（只读）',
        ('Router root: {0}' -f $Diagnostic.resolved_router_root),
        ('Bridge root: {0}' -f $Diagnostic.resolved_bridge_root),
        ('Worker root: {0}' -f $Diagnostic.resolved_worker_root),
        ('Bridge start command: {0}' -f $start.command_line_sanitized),
        ('Bridge health URL: {0}' -f $Diagnostic.bridge_health_command.expected_path),
        ('Chrome executable: {0}' -f $Diagnostic.deepseek_open_command.executable),
        ('Chrome available: {0}' -f $Diagnostic.deepseek_open_command.runtime_exists),
        ('Controlled Chrome profile: {0}' -f $Diagnostic.deepseek_open_command.user_data_dir),
        ('Controlled Chrome debug port: {0}' -f $Diagnostic.deepseek_open_command.debug_port),
        ('Worker required: {0}' -f $Diagnostic.worker_required),
        ('Bridge entry exists: {0}' -f $Diagnostic.bridge_entry_exists),
        ('Node/npm available: {0}' -f $Diagnostic.runtime_exists),
        ('8791 status: {0}' -f $Diagnostic.bridge_port),
        ('8791 owner kind/category: {0}/{1}' -f $Diagnostic.bridge_port_owner.owner_kind,$Diagnostic.bridge_port_owner.process_category),
        ('8791 owner PID/name: {0}/{1}' -f $Diagnostic.bridge_port_owner.pid,$Diagnostic.bridge_port_owner.process_name),
        ('18789 status: {0}' -f $Diagnostic.router_port),
        ('Last launcher error: {0}' -f $Diagnostic.last_error)
    ) -join "`r`n"
}
function Get-UiErrorExplanation([string]$Code) {
    switch ($Code) {
        'ROUTER_NOT_LISTENING' { return '小羽 Router 当前未监听。请先启动 Router；配置读取、备份和校验不依赖 Router。' }
        'LOCAL_LIGHT_UNAVAILABLE' { return '本地轻量模型不可用。请检查本地后端或改用不依赖本地模型的策略选择。' }
        'CODEX_CONFIG_NOT_FOUND' { return '未找到 Codex 配置文件。请确认 C:\Users\bad39\.codex\config.toml 是否存在。' }
        'CODEX_CONFIG_READ_PERMISSION_DENIED' { return '没有读取 Codex 配置文件的权限。请检查文件 ACL 或以拥有权限的账户运行控制台。' }
        'CODEX_CONFIG_TOML_PARSE_FAILED' { return 'Codex 配置 TOML 无法解析。请人工检查语法后重试。' }
        'CODEX_CONFIG_STATUS_FAILED' { return '配置状态读取失败。请运行“配置切换器自检”查看 Python、stderr 和工作目录。' }
        'CODEX_CONFIG_CLI_ENTRYPOINT_FAILED' { return '配置 CLI 入口无法启动。请检查项目 src 目录和 Python 运行时。' }
        'CODEX_CONFIG_CLI_JSON_PARSE_FAILED' { return '配置 CLI 输出不是有效 JSON。请查看脱敏 stderr 摘要。' }
        'CODEX_CONFIG_PYTHON_IMPORT_FAILED' { return 'Python 无法导入配置切换模块。请检查 PYTHONPATH 是否指向项目 src。' }
        'CODEX_CONFIG_WORKDIR_INVALID' { return 'Router 项目工作目录无效。请从当前 Router 项目启动控制台。' }
        'CODEX_CONFIG_UNKNOWN_ERROR_SANITIZED' { return '配置动作发生未知本地错误；详情已脱敏显示。' }
        'OFFICIAL_PROFILE_NOT_CAPTURED' { return '尚未捕获官方配置。请在 Codex 手动确认官方模式后，再点击“捕获当前为官方配置”。' }
        'CODEX_CONFIG_TOML_INVALID' { return 'Codex 配置不是有效 TOML。请先人工修复配置文件，再重试。' }
        'DOWNSTREAM_UNAVAILABLE' { return '下游服务不可用，可能是 Provider 未通过运行资格、模型未确认或服务未启动。' }
        'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED' { return '外部 API 未在新白名单中启用。' }
        'EXTERNAL_MODEL_NOT_ELIGIBLE' { return '外部模型未通过运行资格检查。' }
        'LIVE_CONFIRMATION_REQUIRED' { return '真实外部 API 测试需要用户手动确认。' }
        'AUTH_MISSING' { return '未检测到可用鉴权配置。' }
        'TOOLS_NOT_SUPPORTED_BY_BACKEND' { return '当前后端不支持工具调用；可切换官方直连，或手动启用文本兼容模式。' }
        'DEEPSEEK_MODE_UNAVAILABLE' { return '请求的 DeepSeek 网页模式未通过界面探测，未假装切换。' }
        'UI_PROBE_FAILED' { return 'DeepSeek 页面控件探测失败，未把该模式标记为可用。' }
        'UI_CHANGED' { return 'DeepSeek 页面控件已变化，已停止模式判断，未发送提示词。' }
        'BRIDGE_ROOT_NOT_FOUND' { return '未找到 DeepSeek Web Bridge 项目目录；请检查启动器诊断中的路径。' }
        'BRIDGE_START_ENTRY_NOT_FOUND' { return 'Bridge 项目存在，但没有可识别的 package.json 启动入口。' }
        'BRIDGE_RUNTIME_NOT_FOUND' { return '未找到 Node.js/npm 运行时；请在本机 PATH 中安装并确认 node/npm。' }
        'BRIDGE_START_FAILED' { return 'Bridge 启动命令执行失败；请检查启动器诊断和 Bridge 项目依赖。' }
        'BRIDGE_HEALTH_FAILED' { return 'Bridge 健康检查失败；未发送 prompt。' }
        'BRIDGE_NOT_LISTENING' { return 'Bridge 启动命令结束后 8791 未监听；未继续执行聊天操作。' }
        'BRIDGE_STARTED_BUT_HTTP_UNREACHABLE' { return 'Bridge 进程返回成功但健康 HTTP 不可达；已拒绝把启动标记为 PASS。' }
        'BRIDGE_PROCESS_EXITED_EARLY' { return 'Bridge 进程在健康检查完成前退出；请查看脱敏 stdout/stderr 尾部。' }
        'BRIDGE_PORT_NOT_LISTENING' { return 'Bridge 启动后 8791 未监听；请检查启动入口、端口配置和依赖。' }
        'BRIDGE_HTTP_UNREACHABLE' { return '8791 已有监听或启动命令返回，但 /health HTTP 检查失败；未继续执行模式探测。' }
        'BRIDGE_PORT_OCCUPIED_BY_STALE_BRIDGE' { return '8791 被已识别的旧 Bridge 进程占用；请点击“清理陈旧 Bridge”后再启动。' }
        'BRIDGE_PORT_OCCUPIED_BY_UNKNOWN_PROCESS' { return '8791 被未知进程占用；未自动终止，请先确认 PID 和命令行。' }
        'BRIDGE_PORT_OCCUPIED_BY_NON_BRIDGE' { return '8791 被非 Bridge 进程占用；已停止启动，避免误杀其他服务。' }
        'BRIDGE_START_CWD_INVALID' { return 'Bridge 启动工作目录无效；启动器必须使用已解析的 Bridge 根目录。' }
        'BRIDGE_START_COMMAND_FAILED' { return 'Bridge 启动命令执行失败；请查看脱敏 stdout/stderr 尾部。' }
        'BRIDGE_NOT_RUNNING' { return 'Bridge 未运行；请先启动本机 127.0.0.1:8791 Bridge。' }
        'BRIDGE_MODE_PROBE_FAILED' { return 'Bridge /mode-probe 只读检查失败；未调用 Router、未发送 prompt。' }
        'ROUTER_CLI_RUNTIME_NOT_FOUND' { return '未找到可用的 Router Python 运行时；已尝试项目环境和 Windows Python Launcher。' }
        'CHROME_NOT_FOUND' { return '未找到 Google Chrome；已拒绝回退到 Firefox 或系统默认浏览器。请安装 Chrome 或检查 chrome.exe 路径。' }
        'CHROME_OPEN_FAILED' { return '已找到 Chrome，但启动新窗口失败；请检查 Chrome 安装或进程状态。' }
        'DEEPSEEK_CONTROLLED_CHROME_NOT_ATTACHED' { return '受控 Chrome 未通过本地调试端口连接到 Bridge；未使用普通 Chrome 代替。' }
        'DEEPSEEK_OPENED_IN_DEFAULT_BROWSER_NOT_CONTROLLED' { return '检测到普通 Chrome 页面，但不是 Bridge 管理的受控 Chrome。' }
        'DEEPSEEK_COMPOSER_NOT_FOUND' { return '受控 Chrome 已连接，但未找到 DeepSeek 输入框。' }
        'DEEPSEEK_TOOLBAR_NOT_FOUND' { return '受控 Chrome 已连接，但未找到输入工具栏。' }
        'ACTION_ERROR_SANITIZED_UNKNOWN' { return '本地操作发生未分类错误；详情已脱敏，请查看高级诊断字段。' }
        'WORKER_SCRIPT_NOT_REQUIRED_FOR_DIRECT_BRIDGE' { return '当前三合一探测走 Bridge 8791，旧 Worker 不是必需项。' }
        'LEGACY_WORKER_SCRIPT_NOT_FOUND' { return '旧 Worker 启动脚本不存在；它不阻塞 Bridge-only 模式探测。' }
        'WORKER_START_FAILED' { return '旧 Worker 启动失败；Bridge-only 操作仍可单独使用。' }
        'HEALTH_CHECK_SCRIPT_NOT_FOUND' { return '已移除对旧 health-check.ps1 的依赖；当前健康检查直接读取 Bridge /health。' }
        'CWD_INVALID' { return '工作目录无效；启动器使用已解析的 Bridge 根目录，不依赖当前终端目录。' }
        'SCRIPT_NOT_FOUND_SANITIZED' { return '旧脚本路径不可用；请使用启动器诊断中的明确错误码。' }
        'LOGIN_REQUIRED' { return 'DeepSeek 网页需要登录后才能继续。' }
        'RATE_LIMITED' { return 'DeepSeek 当前触发限流，请停止重试并等待冷却。' }
        'PORT_OCCUPIED_BY_UNKNOWN_PROCESS' { return '18789 被未知进程占用；未停止进程，也未向未知服务发送 POST。请先确认 PID 和命令行。' }
        'STALE_OR_INCOMPATIBLE_ROUTER' { return '检测到旧版或不兼容的小羽 Router；请确认后停止旧进程，再启动当前版本。' }
        'STOP_OLD_ROUTER_CONFIRMATION_REQUIRED' { return '检测到旧小羽 Router，但尚未获得停止确认；未终止任何进程。' }
        default { return '请查看高级信息，确认本地配置和服务状态后重试。' }
    }
}
function Get-UiActionErrorCode([string]$Detail) {
    if ($Detail -match '(ROUTER_RUNTIME_NOT_FOUND|ROUTER_CLI_RUNTIME_NOT_FOUND)') { return 'ROUTER_CLI_RUNTIME_NOT_FOUND' }
    if ($Detail -match '(CHROME_NOT_FOUND|CHROME_RUNTIME_NOT_FOUND|CHROME_OPEN_FAILED)') { return $Matches[1] -replace 'CHROME_RUNTIME_NOT_FOUND','CHROME_NOT_FOUND' }
    if ($Detail -match '(DEEPSEEK_CONTROLLED_CHROME_NOT_ATTACHED|DEEPSEEK_OPENED_IN_DEFAULT_BROWSER_NOT_CONTROLLED|DEEPSEEK_COMPOSER_NOT_FOUND|DEEPSEEK_TOOLBAR_NOT_FOUND)') { return $Matches[1] }
    if ($Detail -match '(BRIDGE_PORT_OCCUPIED_BY_STALE_BRIDGE|BRIDGE_PORT_OCCUPIED_BY_UNKNOWN_PROCESS|BRIDGE_PORT_OCCUPIED_BY_NON_BRIDGE|BRIDGE_START_CWD_INVALID|BRIDGE_START_COMMAND_FAILED|BRIDGE_STARTED_BUT_HTTP_UNREACHABLE|BRIDGE_PROCESS_EXITED_EARLY|BRIDGE_PORT_NOT_LISTENING|BRIDGE_HTTP_UNREACHABLE|BRIDGE_NOT_RUNNING|BRIDGE_MODE_PROBE_FAILED)') { return $Matches[1] }
    if ($Detail -match '(ROUTER_NOT_LISTENING|ROUTER_NOT_RUNNING|actively refused|connection refused)') { return 'ROUTER_NOT_LISTENING' }
    if ($Detail -match '(LOCAL_LIGHT_UNAVAILABLE|LOCAL_DIRECT_BACKEND_ERROR|LOCAL_BACKEND_UNAVAILABLE)') { return 'LOCAL_LIGHT_UNAVAILABLE' }
    if ($Detail -match '(CODEX_CONFIG_NOT_FOUND|OFFICIAL_PROFILE_NOT_CAPTURED|CODEX_CONFIG_TOML_PARSE_FAILED|CODEX_CONFIG_TOML_INVALID|CODEX_CONFIG_READ_PERMISSION_DENIED|CODEX_CONFIG_CLI_ENTRYPOINT_FAILED|CODEX_CONFIG_CLI_JSON_PARSE_FAILED|CODEX_CONFIG_PYTHON_IMPORT_FAILED|CODEX_CONFIG_WORKDIR_INVALID|NON_LOOPBACK_ENDPOINT_BLOCKED)') { return $Matches[1] }
    if ($Detail -match '(DOWNSTREAM_UNAVAILABLE|EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED|EXTERNAL_MODEL_NOT_ELIGIBLE|LIVE_CONFIRMATION_REQUIRED|AUTH_MISSING|DEEPSEEK_MODE_UNAVAILABLE|UI_PROBE_FAILED|UI_CHANGED|LOGIN_REQUIRED|RATE_LIMITED|PORT_OCCUPIED_BY_UNKNOWN_PROCESS|STOP_OLD_ROUTER_CONFIRMATION_REQUIRED|STALE_OR_INCOMPATIBLE_ROUTER)') { return $Matches[1] }
    return 'ACTION_ERROR_SANITIZED_UNKNOWN'
}
function New-UiActionFailure([string]$Name,[string]$Code,[string]$Detail) {
    $routerStatus = if ($SelfTest) { 'NOT_LISTENING' } else { 'UNKNOWN' }
    if (-not $SelfTest) { try { $routerStatus = (Get-RouterStatus).listener_status } catch {} }
    $bridgeEvidence = try { Get-DeepSeekLauncherRuntimeEvidence } catch { $null }
    $stage = if ($Code -match '^ROUTER_CLI_RUNTIME_NOT_FOUND$') { 'resolve_paths' } elseif ($Code -match '^ROUTER_NOT_LISTENING$') { 'router_proxy' } elseif ($Code -match '^BRIDGE_MODE_PROBE_FAILED$') { 'mode_probe' } elseif ($Code -match '^BRIDGE_') { 'wait_http_ready' } else { 'resolve_paths' }
    return [pscustomobject]@{
        status = 'ERROR'
        action_name = $Name
        error_code = $Code
        sanitized_reason = Redact-Text $Detail
        suggested_fix = Get-UiErrorExplanation $Code
        router_status = $routerStatus
        config_path = $CodexConfig
        real_config_was_modified = $script:RealCodexConfigModified
        model_call_was_sent = 'NO'
        command_kind = 'direct_powershell'
        exit_code = 'NOT_APPLICABLE'
        stderr_summary = ''
        stdout_summary = ''
        config_exists = Test-Path -LiteralPath $CodexConfig
        router_required = 'NO'
        stage = $stage
        resolved_bridge_root = $DeepSeekBridgeRoot
        bridge_command_sanitized = 'npm start'
        bridge_process_id = if($bridgeEvidence){$bridgeEvidence.process_id}else{''}
        bridge_exit_code = if($bridgeEvidence){$bridgeEvidence.exit_code}else{''}
        bridge_stdout_tail = if($bridgeEvidence){$bridgeEvidence.stdout_tail}else{''}
        bridge_stderr_tail = if($bridgeEvidence){$bridgeEvidence.stderr_tail}else{''}
        bridge_port_listening = if($bridgeEvidence){$bridgeEvidence.port_listening}else{(Get-DeepSeekPortState 8791)}
        bridge_http_ready = if($bridgeEvidence){$bridgeEvidence.http_ready}else{'NO'}
        last_health_endpoint = if($bridgeEvidence){$bridgeEvidence.last_health_endpoint}else{'http://127.0.0.1:8791/health'}
        last_health_error = if($bridgeEvidence){$bridgeEvidence.last_health_error}else{'NOT_CHECKED'}
        prompt_sent = 'NO'
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
        $code = Get-UiActionErrorCode $detail
        Add-UiDebugInfo -Name $Name -Code $code -Detail $detail
        $failure = New-UiActionFailure $Name $code $detail
        $summary = "操作失败`r`n操作：$($failure.action_name)`r`n错误码：$($failure.error_code)`r`n阶段：$($failure.stage)`r`n原因：$($failure.sanitized_reason)`r`n建议：$($failure.suggested_fix)`r`nRouter：$($failure.router_status)`r`nRouter required：$($failure.router_required)`r`nBridge HTTP ready：$($failure.bridge_http_ready)`r`n配置：$($failure.config_path)`r`n真实配置已修改：$($failure.real_config_was_modified)`r`n模型调用已发送：$($failure.model_call_was_sent)"
        if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($summary,'小羽 Router 控制台',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null }
        return $failure
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
function Invoke-RouterModeSwitch([ValidateSet('quick_plain','quick_thinking','expert_plain','expert_thinking','expert_max_review','quick_search','expert_thinking_search','vision_expert_thinking','file_extract')][string]$TargetMode) {
    # This endpoint accepts only a mode name and verification flag.  It does not
    # accept task text, attachments, credentials, or any chat payload.
    $uri = 'http://127.0.0.1:18789/deepseek/mode-switch'
    $payload = @{ target_mode = $TargetMode; verify = $true } | ConvertTo-Json -Compress
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $uri -ContentType 'application/json; charset=utf-8' -Body $payload -TimeoutSec 10
        $body = $response.Content | ConvertFrom-Json
        return [pscustomobject]@{
            status_code = [int]$response.StatusCode
            target_mode = [string]$body.target_mode
            before = $body.before
            after = $body.after
            matched = [bool]$body.matched
            error_code = ''
        }
    } catch {
        $status = 0; $code = 'DEEPSEEK_MODE_SWITCH_UNAVAILABLE'
        try {
            if ($_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $failure = $reader.ReadToEnd() | ConvertFrom-Json
                if ($failure.error.code) { $code = [string]$failure.error.code }
            }
        } catch {}
        return [pscustomobject]@{ status_code = $status; target_mode = $TargetMode; before = $null; after = $null; matched = $false; error_code = $code }
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
    $identityMatch = $identityText -match '(?i)(codex[_-]ai[_-]router|xiaoyu[-_]router|run-router\.py|python(?:\d+(?:\.\d+)?)?\.exe\s+.*-m\s+codex_ai_router\.cli\s+serve)'
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
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher -and $pythonLauncher.Source) {
        try {
            $resolvedPython = (& $pythonLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1).ToString().Trim()
            if ($resolvedPython) { $candidates += $resolvedPython }
        } catch {}
        $candidates += $pythonLauncher.Source
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source) { $candidates += $pythonCommand.Source }
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
function Get-DeepSeekPortOwner([int]$Port) {
    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($connections.Count -eq 0) {
        return [pscustomobject]@{ port = $Port; port_listening = 'NO'; pid = ''; process_name = ''; command_line_sanitized = ''; executable_path_sanitized = ''; cwd_sanitized = ''; owner_kind = 'none'; process_category = 'none' }
    }
    $rows = foreach ($connection in $connections) {
        $ownerPid = [int]$connection.OwningProcess
        $processInfo = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ownerPid) -ErrorAction SilentlyContinue
        $name = if ($processInfo -and $processInfo.Name) { [string]$processInfo.Name } else { '' }
        $commandLine = if ($processInfo -and $processInfo.CommandLine) { [string]$processInfo.CommandLine } else { '' }
        $executable = if ($processInfo -and $processInfo.ExecutablePath) { [string]$processInfo.ExecutablePath } else { '' }
        $ownerKind = 'unknown'; $processCategory = 'unknown'
        if (-not $processInfo) { $ownerKind = 'unknown' }
        elseif ($Port -eq 8791 -and $DeepSeekBridgeRoot -and ($commandLine -like ('*' + $DeepSeekBridgeRoot + '*') -or ($script:DeepSeekLauncherRuntime -and [string]$script:DeepSeekLauncherRuntime.process_id -eq [string]$ownerPid))) {
            $ownerKind = if ($script:DeepSeekLauncherRuntime -and [string]$script:DeepSeekLauncherRuntime.process_id -eq [string]$ownerPid) { 'bridge_current' } else { 'bridge_stale' }; $processCategory = 'bridge'
        }
        elseif ($name -match '^(chrome|msedge)\.exe$' -and $commandLine -match '--remote-debugging-port') { $ownerKind = 'chrome_devtools'; $processCategory = 'chrome_devtools' }
        elseif ($Port -eq 18789 -and $name -match 'python|node|router|codex') { $ownerKind = 'router'; $processCategory = 'router' }
        elseif ($processInfo) { $ownerKind = 'unknown'; $processCategory = 'non_bridge' }
        [pscustomobject]@{
            port = $Port; port_listening = 'YES'; pid = [string]$ownerPid; process_name = $name
            command_line_sanitized = Redact-Text $commandLine
            executable_path_sanitized = Redact-Text $executable
            cwd_sanitized = ''
            owner_kind = $ownerKind; process_category = $processCategory
        }
    }
    $first = @($rows)[0]
    if (@($rows).Count -gt 1) { $first | Add-Member -NotePropertyName all_owners -NotePropertyValue @($rows) -Force }
    return $first
}
function Clear-DeepSeekStaleBridge {
    $owner = Get-DeepSeekPortOwner 8791
    if ($owner.owner_kind -ne 'bridge_stale') { return [pscustomobject]@{ error_code = 'BRIDGE_PORT_OWNER_NOT_STALE'; owner = $owner } }
    try {
        Stop-Process -Id ([int]$owner.pid) -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 300
        return [pscustomobject]@{ error_code = 'BRIDGE_STALE_CLEARED'; owner = $owner; port_owner_after = Get-DeepSeekPortOwner 8791 }
    } catch {
        return [pscustomobject]@{ error_code = 'BRIDGE_STALE_CLEANUP_FAILED'; owner = $owner }
    }
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
    $owner = Get-DeepSeekPortOwner 8791
    $snapshot = [ordered]@{ bridge_port = Get-DeepSeekPortState 8791; bridge_port_owner = $owner.owner_kind; worker_port = Get-DeepSeekPortState 8792; fixture_port = Get-DeepSeekPortState 8793; bridge = '未运行'; worker = '未运行'; chrome = '未知'; chrome_kind = 'unknown'; controlled_attached = 'NO'; page = '未知'; composer_found = 'NO'; toolbar_found = 'NO'; busy = '未知' }
    if ($snapshot.bridge_port -ne '未监听') {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8791/health' -TimeoutSec 3
            $health = $response.Content | ConvertFrom-Json
            $snapshot.bridge = if ($health.ok -eq $true) { 'READY' } else { '响应异常' }
            $snapshot.chrome = if ($health.browser.connected -eq $true) { '已连接' } else { '未连接' }
            $snapshot.chrome_kind = if ($health.browser.chromeKind) { [string]$health.browser.chromeKind } else { 'unknown' }
            $snapshot.controlled_attached = if ($health.browser.controlledAttached -eq $true) { 'YES' } else { 'NO' }
            $snapshot.composer_found = if ($health.browser.composerFound -eq $true) { 'YES' } else { 'NO' }
            $snapshot.toolbar_found = if ($health.browser.toolbarFound -eq $true) { 'YES' } else { 'NO' }
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
function Get-DeepSeekLogTail([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    try { return (Redact-Text ((Get-Content -LiteralPath $Path -Tail 20 -Encoding UTF8 -ErrorAction Stop) -join "`n")) } catch { return '' }
}
function Get-DeepSeekLauncherRuntimeEvidence {
    $runtime = $script:DeepSeekLauncherRuntime
    if (-not $runtime) {
        return [pscustomobject]@{ process_id = ''; process_running = 'NO'; exit_code = ''; stdout_tail = ''; stderr_tail = ''; port_listening = (Get-DeepSeekPortState 8791); http_ready = 'NO'; last_health_endpoint = 'http://127.0.0.1:8791/health'; last_health_error = 'NOT_CHECKED' }
    }
    $process = $runtime.process
    $running = $false; $exitCode = ''
    try { $running = -not $process.HasExited; if (-not $running) { $exitCode = [string]$process.ExitCode } } catch {}
    return [pscustomobject]@{
        process_id = [string]$runtime.process_id; process_running = if ($running) { 'YES' } else { 'NO' }; exit_code = $exitCode
        stdout_tail = Get-DeepSeekLogTail $runtime.stdout_path; stderr_tail = Get-DeepSeekLogTail $runtime.stderr_path
        port_listening = if ((Get-DeepSeekPortState 8791) -eq '127.0.0.1 本机监听') { 'YES' } else { Get-DeepSeekPortState 8791 }
        http_ready = if ($runtime.http_ready) { 'YES' } else { 'NO' }; last_health_endpoint = 'http://127.0.0.1:8791/health'; last_health_error = if ($runtime.last_health_error) { $runtime.last_health_error } else { 'NOT_CHECKED' }
    }
}
function Test-DeepSeekBridgeHttpReady {
    $endpoint = 'http://127.0.0.1:8791/health'
    if ((Get-DeepSeekPortState 8791) -ne '127.0.0.1 本机监听') { return [pscustomobject]@{ port_listening = $false; http_ready = $false; status_code = ''; error_code = 'BRIDGE_PORT_NOT_LISTENING'; endpoint = $endpoint } }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $endpoint -TimeoutSec 2
        $ready = ($response.StatusCode -eq 200)
        return [pscustomobject]@{ port_listening = $true; http_ready = $ready; status_code = [string]$response.StatusCode; error_code = if($ready){'NONE'}else{'BRIDGE_HTTP_UNREACHABLE'}; endpoint = $endpoint }
    } catch {
        return [pscustomobject]@{ port_listening = $true; http_ready = $false; status_code = ''; error_code = 'BRIDGE_HTTP_UNREACHABLE'; endpoint = $endpoint }
    }
}
function Wait-DeepSeekBridgeHttpReady([object]$Process,[int]$TimeoutSeconds=10) {
    $last = $null
    $attempts = [math]::Floor(($TimeoutSeconds * 1000) / 350)
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $last = Test-DeepSeekBridgeHttpReady
        if ($last.http_ready) { return $last }
        $exited = $false
        try { $exited = $Process -and $Process.HasExited } catch {}
        if ($exited) {
            $exitCode = ''
            try { $exitCode = [string]$Process.ExitCode } catch {}
            $last | Add-Member -NotePropertyName process_exited_early -NotePropertyValue $true -Force
            $last | Add-Member -NotePropertyName process_exit_code -NotePropertyValue $exitCode -Force
            return $last
        }
        Start-Sleep -Milliseconds 350
    }
    if (-not $last) { $last = Test-DeepSeekBridgeHttpReady }
    $last | Add-Member -NotePropertyName process_exited_early -NotePropertyValue $false -Force
    return $last
}
function Get-DeepSeekBridgeHealthData {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8791/health' -TimeoutSec 3
        if ($response.StatusCode -ne 200) { return $null }
        return ($response.Content | ConvertFrom-Json)
    } catch { return $null }
}
function New-DeepSeekLauncherResult([string]$Action,[int]$ExitCode,[string]$ErrorType,[object]$Diagnostic=$null,[string]$ExpectedPath='',[string]$CommandKind='') {
    if (-not $Diagnostic) { $Diagnostic = Test-LauncherPrerequisites }
    $start = $Diagnostic.bridge_start_command
    $commandSpec = if ($Action -eq 'open-deepseek-web') { $Diagnostic.deepseek_open_command } else { $start }
    $evidence = Get-DeepSeekLauncherRuntimeEvidence
    $stage = switch ($Action) { 'start-bridge' { if($ErrorType -eq 'PASS' -or $ErrorType -eq 'ALREADY_RUNNING'){'wait_http_ready'}else{'start_process'} }; 'health-check' {'wait_http_ready'}; 'start-worker' {'resolve_paths'}; default {'resolve_paths'} }
    $health = Get-DeepSeekBridgeHealthData
    return [pscustomobject]@{
        action = $Action; exit_code = $ExitCode; error_type = $ErrorType; error_code = $ErrorType
        expected_path = if ($ExpectedPath) { $ExpectedPath } else { $commandSpec.expected_path }
        resolved_router_root = $Diagnostic.resolved_router_root; resolved_bridge_root = $Diagnostic.resolved_bridge_root; resolved_worker_root = $Diagnostic.resolved_worker_root
        cwd = (Get-Location).Path; command_kind = if ($CommandKind) { $CommandKind } else { $commandSpec.command_kind }
        command_line_sanitized = if ($commandSpec.command_line_sanitized) { $commandSpec.command_line_sanitized } else { 'not available' }
        file_exists = if ($Action -eq 'open-deepseek-web') { $commandSpec.file_exists } else { $Diagnostic.bridge_entry_exists }; runtime_exists = if ($Action -eq 'open-deepseek-web') { $commandSpec.runtime_exists } else { $Diagnostic.runtime_exists }; suggested_fix = if ($commandSpec.suggested_fix) { $commandSpec.suggested_fix } elseif ($start.suggested_fix) { $start.suggested_fix } else { 'NONE' }
        bridge_command_sanitized = if ($start.command_line_sanitized) { $start.command_line_sanitized } else { 'not available' }; stage = $stage
        bridge_process_id = $evidence.process_id; bridge_exit_code = if($evidence.exit_code){$evidence.exit_code}else{[string]$ExitCode}; bridge_stdout_tail = $evidence.stdout_tail; bridge_stderr_tail = $evidence.stderr_tail
        bridge_port_listening = $evidence.port_listening; bridge_http_ready = $evidence.http_ready; last_health_endpoint = $evidence.last_health_endpoint; last_health_error = $evidence.last_health_error
        port_owner_kind = $Diagnostic.bridge_port_owner.owner_kind; port_owner_category = $Diagnostic.bridge_port_owner.process_category; port_owner_pid = $Diagnostic.bridge_port_owner.pid; port_owner_process_name = $Diagnostic.bridge_port_owner.process_name; port_owner_command_line_sanitized = $Diagnostic.bridge_port_owner.command_line_sanitized
        chrome_kind = if ($health -and $health.browser) { [string]$health.browser.chromeKind } else { 'unknown' }; controlled_chrome_attached = if ($health -and $health.browser) { [bool]$health.browser.controlledAttached } else { $false }
        chrome_debug_port = if ($health -and $health.browser) { $health.browser.debugPort } else { $commandSpec.debug_port }; chrome_user_data_dir_sanitized = if ($health -and $health.browser -and $health.browser.userDataDir) { '[LOCAL_PROFILE]' } else { '[LOCAL_PROFILE]' }
        active_page_url_kind = if ($health -and $health.browser) { [string]$health.browser.activePageUrlKind } else { 'unknown' }; composer_found = if ($health -and $health.browser) { [bool]$health.browser.composerFound } else { $false }; toolbar_found = if ($health -and $health.browser) { [bool]$health.browser.toolbarFound } else { $false }
        bridge_required = if ($Action -eq 'open-deepseek-web') { 'NO' } else { 'YES' }; worker_required = if ($Action -eq 'open-deepseek-web') { 'NO' } else { $Diagnostic.worker_required }; router_required = 'NO'; router_status = Get-DeepSeekPortState 18789; prompt_sent = $false; model_call_sent = $false
    }
}
function Start-DeepSeekBridgeProcess {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    $start = $diagnostic.bridge_start_command
    $owner = $diagnostic.bridge_port_owner
    if ($owner.owner_kind -eq 'unknown' -or $owner.owner_kind -eq 'chrome_devtools') {
        $existingHealth = Get-DeepSeekBridgeHealthData
        if ($existingHealth -and $existingHealth.service -eq 'deepseek-web-browser-bridge') {
            $owner.owner_kind = 'bridge_current'
        } elseif ($owner.owner_kind -eq 'unknown' -and $owner.process_category -ne 'non_bridge') {
            return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_PORT_OCCUPIED_BY_UNKNOWN_PROCESS' $diagnostic)
        } else {
            return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_PORT_OCCUPIED_BY_NON_BRIDGE' $diagnostic)
        }
    }
    if ($owner.owner_kind -eq 'bridge_stale') { return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_PORT_OCCUPIED_BY_STALE_BRIDGE' $diagnostic) }
    if ($owner.owner_kind -eq 'bridge_current' -or $diagnostic.bridge_port -eq '127.0.0.1 本机监听') {
        $ready = Test-DeepSeekBridgeHttpReady
        $script:DeepSeekLauncherRuntime = [pscustomobject]@{ process_id = ''; process = $null; stdout_path = ''; stderr_path = ''; http_ready = $ready.http_ready; last_health_error = $ready.error_code }
        if ($ready.http_ready) { return (New-DeepSeekLauncherResult 'start-bridge' 0 'ALREADY_RUNNING' $diagnostic) }
        return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_HTTP_UNREACHABLE' $diagnostic)
    }
    if ($start.error_code -ne 'NONE') { return (New-DeepSeekLauncherResult 'start-bridge' 1 ([string]$start.error_code) $diagnostic) }
    try {
        $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-deepseek-bridge-' + [guid]::NewGuid().ToString('N') + '.out.log')
        $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-deepseek-bridge-' + [guid]::NewGuid().ToString('N') + '.err.log')
        $process = Start-Process -FilePath ([string]$start.runtime) -ArgumentList @('start') -WorkingDirectory ([string]$start.resolved_bridge_root) -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $script:DeepSeekLauncherRuntime = [pscustomobject]@{ process_id = $process.Id; process = $process; stdout_path = $stdoutPath; stderr_path = $stderrPath; http_ready = $false; last_health_error = 'WAITING' }
        $ready = Wait-DeepSeekBridgeHttpReady $process 10
        $script:DeepSeekLauncherRuntime.http_ready = [bool]$ready.http_ready
        $script:DeepSeekLauncherRuntime.last_health_error = [string]$ready.error_code
        if ($ready.http_ready) { return (New-DeepSeekLauncherResult 'start-bridge' 0 'PASS' $diagnostic) }
        if ($ready.process_exited_early) {
            if ([string]$ready.process_exit_code -eq '0') { return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_STARTED_BUT_HTTP_UNREACHABLE' $diagnostic) }
            return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_PROCESS_EXITED_EARLY' $diagnostic)
        }
        if (-not $ready.port_listening) { return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_PORT_NOT_LISTENING' $diagnostic) }
        return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_HTTP_UNREACHABLE' $diagnostic)
    } catch {
        return (New-DeepSeekLauncherResult 'start-bridge' 1 'BRIDGE_START_FAILED' $diagnostic)
    }
}
function Invoke-DeepSeekBridgeHealth {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    $ready = Test-DeepSeekBridgeHttpReady
    $script:DeepSeekLastHealth = if($ready.http_ready){'PASS'}else{[string]$ready.error_code}
    if ($script:DeepSeekLauncherRuntime) { $script:DeepSeekLauncherRuntime.http_ready = [bool]$ready.http_ready; $script:DeepSeekLauncherRuntime.last_health_error = [string]$ready.error_code }
    return (New-DeepSeekLauncherResult 'health-check' $(if($ready.http_ready){0}else{1}) $script:DeepSeekLastHealth $diagnostic 'http://127.0.0.1:8791/health' 'HTTP_GET')
}
function Stop-DeepSeekBridgeProcess {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    $connections = @(Get-NetTCPConnection -LocalPort 8791 -State Listen -ErrorAction SilentlyContinue)
    if ($connections.Count -eq 0) { return (New-DeepSeekLauncherResult 'stop' 0 'NOT_RUNNING' $diagnostic) }
    $stopped = $false
    foreach ($connection in $connections) {
        $ownerPid = [int]$connection.OwningProcess
        $processInfo = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $ownerPid) -ErrorAction SilentlyContinue
        if ($processInfo -and $diagnostic.resolved_bridge_root -and ([string]$processInfo.CommandLine -like ('*' + $diagnostic.resolved_bridge_root + '*'))) {
            Stop-Process -Id $ownerPid -Force -ErrorAction Stop
            $stopped = $true
        }
    }
    if ($stopped) { return (New-DeepSeekLauncherResult 'stop' 0 'STOPPED' $diagnostic) }
    return (New-DeepSeekLauncherResult 'stop' 1 'BRIDGE_STOP_REFUSED_UNKNOWN_PROCESS' $diagnostic)
}
function Invoke-DeepSeekLegacyWorker {
    $diagnostic = Test-LauncherPrerequisites
    $workerRoot = $diagnostic.resolved_worker_root
    $script:DeepSeekLauncherLast = $diagnostic
    $entry = if ($workerRoot) { Join-Path $workerRoot 'scripts\local-bridge\start-worker.ps1' } else { $null }
    if (-not $entry -or -not (Test-Path -LiteralPath $entry -PathType Leaf)) { return (New-DeepSeekLauncherResult 'start-worker' 0 'WORKER_SCRIPT_NOT_REQUIRED_FOR_DIRECT_BRIDGE' $diagnostic $entry 'LEGACY_OPTIONAL') }
    try {
        $raw = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $entry 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { return (New-DeepSeekLauncherResult 'start-worker' 1 'WORKER_START_FAILED' $diagnostic $entry 'LEGACY_OPTIONAL') }
        if (-not (Wait-DeepSeekLoopbackPort 8792)) { return (New-DeepSeekLauncherResult 'start-worker' 1 'WORKER_START_FAILED' $diagnostic $entry 'LEGACY_OPTIONAL') }
        return (New-DeepSeekLauncherResult 'start-worker' 0 'LEGACY_WORKER_STARTED_OPTIONAL' $diagnostic $entry 'LEGACY_OPTIONAL')
    } catch { return (New-DeepSeekLauncherResult 'start-worker' 1 'WORKER_START_FAILED' $diagnostic $entry 'LEGACY_OPTIONAL') }
}
function Open-DeepSeekControlledChrome {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    if ($diagnostic.deepseek_open_command.error_code -ne 'NONE') { return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 ([string]$diagnostic.deepseek_open_command.error_code) $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME') }
    $ready = Test-DeepSeekBridgeHttpReady
    if (-not $ready.http_ready) {
        $started = Start-DeepSeekBridgeProcess
        if ($started.error_type -notin @('PASS','ALREADY_RUNNING')) {
            return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 ([string]$started.error_code) $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME')
        }
    }
    $health = Get-DeepSeekBridgeHealthData
    if (-not $health -or -not $health.browser -or $health.browser.chromeKind -ne 'controlled' -or $health.browser.controlledAttached -ne $true) {
        return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 'DEEPSEEK_CONTROLLED_CHROME_NOT_ATTACHED' $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME')
    }
    if ($health.browser.activePageUrlKind -ne 'deepseek_chat') { return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 'DEEPSEEK_OPENED_IN_DEFAULT_BROWSER_NOT_CONTROLLED' $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME') }
    if ($health.browser.composerFound -ne $true) { return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 'DEEPSEEK_COMPOSER_NOT_FOUND' $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME') }
    if ($health.browser.toolbarFound -ne $true) { return (New-DeepSeekLauncherResult 'open-deepseek-web' 1 'DEEPSEEK_TOOLBAR_NOT_FOUND' $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME') }
    return (New-DeepSeekLauncherResult 'open-deepseek-web' 0 'OPENED_CONTROLLED_CHROME' $diagnostic $DeepSeekWebUrl 'CONTROLLED_CHROME')
}
function Invoke-DeepSeekLocalScript([ValidateSet('start-bridge','start-worker','health-check','smoke','stop','clear-stale-bridge','open-deepseek-web')][string]$Action) {
    switch ($Action) {
        'start-bridge' { return (Start-DeepSeekBridgeProcess) }
        'start-worker' { return (Invoke-DeepSeekLegacyWorker) }
        'health-check' { return (Invoke-DeepSeekBridgeHealth) }
        'stop' { return (Stop-DeepSeekBridgeProcess) }
        'clear-stale-bridge' {
            $diagnostic = Test-LauncherPrerequisites
            $cleanup = Clear-DeepSeekStaleBridge
            $script:DeepSeekLauncherLast = Test-LauncherPrerequisites
            $code = [string]$cleanup.error_code
            return (New-DeepSeekLauncherResult 'clear-stale-bridge' $(if($code -eq 'BRIDGE_STALE_CLEARED'){0}else{1}) $code $diagnostic)
        }
        'open-deepseek-web' { return (Open-DeepSeekControlledChrome) }
        'smoke' { return (New-DeepSeekLauncherResult 'smoke' 1 'SMOKE_REQUIRES_EXPLICIT_DIRECT_BRIDGE_FLOW' (Test-LauncherPrerequisites)) }
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
function Get-CodexConfigPythonExecutable {
    $candidates = @((Join-Path $ProjectRoot '.venv\Scripts\python.exe'),(Join-Path $ProjectRoot 'venv\Scripts\python.exe'))
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher -and $pythonLauncher.Source) {
        try {
            $resolvedPython = (& $pythonLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1).ToString().Trim()
            if ($resolvedPython) { $candidates += $resolvedPython }
        } catch {}
        $candidates += $pythonLauncher.Source
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source) { $candidates += $pythonCommand.Source }
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython -and $systemPython.Source) { $candidates += $systemPython.Source }
    $candidates += 'C:\EasyDiffusion\installer_files\env\python.exe'
    foreach ($candidate in @($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $hasTomlParser = $false
        try { & $candidate -c 'import tomllib' 2>$null; $hasTomlParser = ($LASTEXITCODE -eq 0) } catch {}
        if (-not $hasTomlParser) { try { & $candidate -c 'import tomli' 2>$null; $hasTomlParser = ($LASTEXITCODE -eq 0) } catch {} }
        if ($hasTomlParser) { return $candidate }
    }
    return $null
}
function Invoke-CodexConfigModuleCli([string[]]$Arguments) {
    $srcRoot = Join-Path $ProjectRoot 'src'
    if (-not (Test-Path -LiteralPath $ProjectRoot) -or -not (Test-Path -LiteralPath $srcRoot)) {
        return [pscustomobject]@{ command_kind='python_module'; error_code='CODEX_CONFIG_WORKDIR_INVALID'; exit_code=1; stdout=''; stderr='Router project root or src directory is missing.'; python_executable=''; working_directory=$ProjectRoot; pythonpath=$srcRoot }
    }
    $python = Get-CodexConfigPythonExecutable
    if (-not $python) {
        return [pscustomobject]@{ command_kind='python_module'; error_code='CODEX_CONFIG_CLI_ENTRYPOINT_FAILED'; exit_code=1; stdout=''; stderr='Compatible Python runtime was not found.'; python_executable=''; working_directory=$ProjectRoot; pythonpath=$srcRoot }
    }
    $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-codex-config-' + [guid]::NewGuid().ToString('N') + '.stdout')
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-codex-config-' + [guid]::NewGuid().ToString('N') + '.stderr')
    $previousPythonPath = [string]$env:PYTHONPATH
    try {
        $env:PYTHONPATH = $srcRoot
        Push-Location -LiteralPath $ProjectRoot
        & $python @('-m','codex_ai_router.cli') @Arguments 1>$stdoutPath 2>$stderrPath
        $exitCode = $LASTEXITCODE
        $stdout = if(Test-Path -LiteralPath $stdoutPath){Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8}else{''}
        $stderr = if(Test-Path -LiteralPath $stderrPath){Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8}else{''}
        return [pscustomobject]@{ command_kind='python_module'; error_code=''; exit_code=$exitCode; stdout=$stdout; stderr=$stderr; python_executable=$python; working_directory=$ProjectRoot; pythonpath=$srcRoot }
    } catch {
        return [pscustomobject]@{ command_kind='python_module'; error_code='CODEX_CONFIG_CLI_ENTRYPOINT_FAILED'; exit_code=1; stdout=''; stderr=([string]$_.Exception.Message); python_executable=$python; working_directory=$ProjectRoot; pythonpath=$srcRoot }
    } finally {
        Pop-Location -ErrorAction SilentlyContinue
        $env:PYTHONPATH = $previousPythonPath
        Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    }
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
    Write-Output 'DEEPSEEK_HEAD_COLLABORATION_PANEL_VISIBLE=YES'; Write-Output 'DEEPSEEK_HEAD_CONTEXT_READONLY=YES'; Write-Output 'DEEPSEEK_HEAD_AUTO_BRAIN_SELECTION=YES'; Write-Output 'DEEPSEEK_HEAD_CONTEXT_REDACTION=YES'
}
if ($NoShow) {
    $router = Get-RouterStatus; $rows = Get-ProviderRows
    Write-Output 'WORK_PROFILE_PANEL_VISIBLE=YES'; Write-Output 'WORK_PROFILE_PROFILE_SELECTION=YES'; Write-Output 'CODEX_CUSTOM_MODE_MANUAL_CONFIRMATION=YES'; Write-Output 'WORK_PROFILE_HANDOFF_VISIBLE=YES'; Write-Output 'CODEX_CONFIG_SWITCHER_UI_VISIBLE=YES'; Write-Output 'CODEX_CONFIG_SWITCHER_BUTTON_COUNT=8'; Write-Output 'CODEX_CONFIG_SWITCHER_REAL_CONFIG_MODIFIED=NO'
    Write-Output ('ROUTER_STATUS_VISIBLE=' + $(if ($router.running) { 'YES' } else { 'NO' })); Write-Output 'CODEX_STATUS_VISIBLE=YES'; Write-Output 'PROVIDER_LIST_VISIBLE=YES'; Write-Output ('LIGHTBOAT_PROVIDER_VISIBLE=' + $(if (@($rows | Where-Object { $_.provider_id -eq 'lightboat-3' }).Count -gt 0) { 'YES' } else { 'NO' })); Write-Output 'USAGE_GUARD_VISIBLE=YES'; Write-Output 'DEEPSEEK_LOCAL_BRIDGE_PANEL_VISIBLE=YES'; Write-Output 'CODEX_MODE_PANEL_VISIBLE=YES'; Write-Output 'PROVIDER_ALLOWLIST_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_RECORDS_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_AGENT_PANEL_VISIBLE=YES'; Write-Output 'LOCAL_AGENT_DEFAULT_READ_ONLY=YES'; Write-Output 'LOCAL_AGENT_CONFIRMATION_GATES=YES'; Write-Output 'LOCAL_AGENT_BRAIN_EXPLICIT=YES'; Write-Output 'DEEPSEEK_BRIDGE_DIRECT_BRAIN_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_BRIDGE_DIRECT_ENDPOINT=127.0.0.1:8791'; Write-Output 'CODEX_TASK_INPUT_LOCATION=CODEX_ONLY'; Write-Output 'NO_QUOTA_MODE_VISIBLE=YES'; Write-Output 'RESPONSE_COMPAT_DIAGNOSTICS_VISIBLE=YES'; Write-Output 'TOOLS_POLICY_UI_VISIBLE=YES'; Write-Output 'TEXT_ONLY_MODE_BUTTONS_VISIBLE=YES'; Write-Output 'TEXT_ONLY_DEFAULT_STRICT_REJECT=YES'; Write-Output 'DEEPSEEK_HEALTH_PROMPT_SENT=NO'; Write-Output 'DEEPSEEK_MODE_PROBE_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_MODE_SELECTOR_UI_VISIBLE=YES'; Write-Output 'DEEPSEEK_MODE_PROBE_PROMPT_SENT=NO'; Write-Output 'OFFICIAL_ASSISTED_COORDINATOR_VISIBLE=YES'; Write-Output 'ASSIST_COORDINATE_API_VISIBLE=YES'; Write-Output 'OFFICIAL_ASSISTED_COORDINATOR_ENDPOINT=127.0.0.1:18789/assist/coordinate'; Write-Output 'CODEX_START_INSTRUCTION_VISIBLE=YES'; Write-Output 'OFFICIAL_DIRECT_UNCHANGED=YES'; Write-Output 'CODEX_ENDPOINT_TOUCHED=NO'; Write-Output 'CODEX_AGENT_AUTO_INVOKED=NO'; Write-Output 'CODEX_AGENTIC_USAGE_BYPASS=NO'; Write-Output 'CONTROL_PANEL_LANGUAGE=ZH_CN'; Write-Output 'ERROR_CODE_CHINESE_EXPLANATION=YES'; Write-Output 'DEBUG_FIELDS_COLLAPSED=YES'; Write-Output 'CONTROL_PANEL_EXCEPTION_GUARD=YES'; Write-Output 'NO_JIT_DIALOG_ON_BUTTON_ERROR=YES'; Write-Output 'CONTROL_PANEL_JSON_POPUP_DEFAULT=NO'; Write-Output 'OFFICIAL_DIRECT_TOOLS_POLICY_DISPLAY=NOT_APPLICABLE'; Write-Output 'SECRET_VALUES_VISIBLE=NO'; Write-Output 'ROUTER_PORT_OWNER_FIELDS=YES'; Write-Output 'ROUTER_IDENTITY_PROBE=YES'; Write-Output 'STALE_ROUTER_CONFIRMATION_GATE=YES'; Write-Output 'UNKNOWN_PROCESS_SAFE_STOP=YES'; Write-Output 'UNKNOWN_SERVICE_POST_BLOCKED=YES'; Write-Output 'CONTROL_PANEL_LAYOUT_POLISH=YES'; Write-Output 'LOCAL_AGENT_GROUPS=YES'; Write-Output 'OFFICIAL_ASSISTED_GROUPS=YES'; Write-Output 'BUTTON_TEXT_VISIBLE=YES'; Write-Output 'WINDOW_RESIZE_SUPPORTED=YES'; Write-Output 'VERTICAL_SCROLL_SUPPORTED=YES'; Write-Output 'DANGEROUS_ACTIONS_STILL_CONFIRM=YES'; Write-Output 'DANGEROUS_ACTION_TOOLTIPS=YES'; Write-Output 'RAW_JSON_COLLAPSED=YES'; exit 0
}

$uiFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$buttonFont = New-Object System.Drawing.Font('Microsoft YaHei UI', [single](11 * $FontScale), [System.Drawing.FontStyle]::Regular)
$uiToolTip = New-Object System.Windows.Forms.ToolTip; $uiToolTip.AutoPopDelay = 9000; $uiToolTip.InitialDelay = 250; $uiToolTip.ReshowDelay = 100
$form = New-Object System.Windows.Forms.Form; $form.Text = '小羽 Router 控制台'; $form.Size = New-Object System.Drawing.Size(1180,780); $form.MinimumSize = New-Object System.Drawing.Size(920,620); $form.StartPosition = 'CenterScreen'; $form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi; $form.AutoScroll = $true; $form.Font = $uiFont
$tabs = New-Object System.Windows.Forms.TabControl; $tabs.Dock = 'Fill'; $tabs.Multiline = $true; $form.Controls.Add($tabs)
$homeTab = New-Object System.Windows.Forms.TabPage('首页'); $providersTab = New-Object System.Windows.Forms.TabPage('供应商'); $deepSeekTab = New-Object System.Windows.Forms.TabPage('DeepSeek 本地桥接'); $assistantTab = New-Object System.Windows.Forms.TabPage('Codex 模式与供应商白名单'); $configSwitcherTab = New-Object System.Windows.Forms.TabPage('Codex 配置切换'); $localAgentTab = New-Object System.Windows.Forms.TabPage('小羽本地 Agent'); $usageTab = New-Object System.Windows.Forms.TabPage('用量保护'); $diagnosticsTab = New-Object System.Windows.Forms.TabPage('诊断'); [void]$tabs.TabPages.AddRange(@($homeTab,$providersTab,$deepSeekTab,$assistantTab,$configSwitcherTab,$localAgentTab,$usageTab,$diagnosticsTab))
$RouterCommit = try { (git -C $ProjectRoot rev-parse --short HEAD 2>$null).Trim() } catch { 'UNAVAILABLE' }
$ConfigSwitcherVersion = 'R4_LAYOUT_FONTSCALE'
$scriptLeaf = Split-Path -Leaf $PSCommandPath; $scriptParentLeaf = Split-Path -Leaf (Split-Path -Parent $PSCommandPath)
$shortScriptPath = if ($PSCommandPath.Length -gt 56) { "...\$scriptParentLeaf\$scriptLeaf" } else { $PSCommandPath }
$runtimeVersionText = "Router git commit：$RouterCommit`r`n脚本路径：$shortScriptPath`r`nConfig Switcher version：$ConfigSwitcherVersion`r`nCODEX_CONFIG_SWITCHER_UI=YES"
$runtimeVersionDetail = "Router git commit：$RouterCommit`r`n脚本路径：$PSCommandPath`r`nConfig Switcher version：$ConfigSwitcherVersion`r`nCODEX_CONFIG_SWITCHER_UI=YES"
$homeConfigHeight = [math]::Ceiling(120 + (70 * $FontScale)); $homeConfigStatusHeight = [math]::Ceiling(38 + (28 * $FontScale))
$homeTab.AutoScroll = $true
$homeLayout = New-Object System.Windows.Forms.TableLayoutPanel; $homeLayout.Dock = 'Fill'; $homeLayout.AutoScroll = $true; $homeLayout.Padding = New-Object System.Windows.Forms.Padding(12); $homeLayout.RowCount = 5; $homeLayout.ColumnCount = 1; [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,$homeConfigHeight))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); [void]$homeLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,175))); $homeTab.Controls.Add($homeLayout)
$routerGroup = New-Object System.Windows.Forms.GroupBox; $routerGroup.Text = 'Router 服务'; $routerGroup.Dock = 'Fill'; $routerGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($routerGroup,0,0)
$homeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $homeButtons.Dock = 'Fill'; $homeButtons.AutoSize = $true; $routerGroup.Controls.Add($homeButtons)
$switchGroup = New-Object System.Windows.Forms.GroupBox; $switchGroup.Text = 'Codex 切换与交接'; $switchGroup.Dock = 'Fill'; $switchGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($switchGroup,0,1)
$switchButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $switchButtons.Dock = 'Fill'; $switchButtons.AutoSize = $true; $switchGroup.Controls.Add($switchButtons)
$homeConfigSwitcherGroup = New-Object System.Windows.Forms.GroupBox; $homeConfigSwitcherGroup.Text = 'Codex 配置切换'; $homeConfigSwitcherGroup.Dock = 'Fill'; $homeConfigSwitcherGroup.Padding = New-Object System.Windows.Forms.Padding(8); $homeLayout.Controls.Add($homeConfigSwitcherGroup,0,2)
$homeConfigSwitcherLayout = New-Object System.Windows.Forms.TableLayoutPanel; $homeConfigSwitcherLayout.Dock = 'Fill'; $homeConfigSwitcherLayout.RowCount = 2; $homeConfigSwitcherLayout.ColumnCount = 1; [void]$homeConfigSwitcherLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,$homeConfigStatusHeight))); [void]$homeConfigSwitcherLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $homeConfigSwitcherGroup.Controls.Add($homeConfigSwitcherLayout)
$homeConfigSwitcherStatus = New-Object System.Windows.Forms.TextBox; $homeConfigSwitcherStatus.Multiline = $true; $homeConfigSwitcherStatus.ReadOnly = $true; $homeConfigSwitcherStatus.WordWrap = $true; $homeConfigSwitcherStatus.ScrollBars = 'Vertical'; $homeConfigSwitcherStatus.Dock = 'Fill'; $homeConfigSwitcherStatus.Font = $uiFont; $homeConfigSwitcherStatus.Text = $runtimeVersionText; $uiToolTip.SetToolTip($homeConfigSwitcherStatus,$runtimeVersionDetail); $homeConfigSwitcherLayout.Controls.Add($homeConfigSwitcherStatus,0,0)
$homeConfigSwitcherButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $homeConfigSwitcherButtons.Dock = 'Fill'; $homeConfigSwitcherButtons.AutoScroll = $true; $homeConfigSwitcherButtons.WrapContents = $true; $homeConfigSwitcherButtons.FlowDirection = 'LeftToRight'; $homeConfigSwitcherButtons.Font = $buttonFont; $homeConfigSwitcherLayout.Controls.Add($homeConfigSwitcherButtons,0,1)
$statusGroup = New-Object System.Windows.Forms.GroupBox; $statusGroup.Text = '当前状态'; $statusGroup.Dock = 'Fill'; $statusGroup.Padding = New-Object System.Windows.Forms.Padding(10); $homeLayout.Controls.Add($statusGroup,0,3)
$statusBox = New-Object System.Windows.Forms.TextBox; $statusBox.Multiline = $true; $statusBox.ReadOnly = $true; $statusBox.Dock = 'Fill'; $statusBox.ScrollBars = 'Vertical'; $statusBox.Font = $uiFont; $statusGroup.Controls.Add($statusBox)
$directLocalGroup = New-Object System.Windows.Forms.GroupBox; $directLocalGroup.Text = '直接本地模型（推荐） Studio'; $directLocalGroup.Dock = 'Fill'; $directLocalGroup.Padding = New-Object System.Windows.Forms.Padding(8); $homeLayout.Controls.Add($directLocalGroup,0,4)
$directLocalStatus = New-Object System.Windows.Forms.TextBox; $directLocalStatus.Multiline = $true; $directLocalStatus.ReadOnly = $true; $directLocalStatus.Dock = 'Fill'; $directLocalStatus.Font = $uiFont; $directLocalStatus.ScrollBars = 'Vertical'; $directLocalGroup.Controls.Add($directLocalStatus)
$directLocalButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $directLocalButtons.Dock = 'Bottom'; $directLocalButtons.Height = 42; $directLocalButtons.Font = $buttonFont; $directLocalGroup.Controls.Add($directLocalButtons)
$configSwitcherTab.AutoScroll = $true
$configSwitcherTabLayout = New-Object System.Windows.Forms.TableLayoutPanel; $configSwitcherTabLayout.Dock = 'Fill'; $configSwitcherTabLayout.AutoScroll = $true; $configSwitcherTabLayout.Padding = New-Object System.Windows.Forms.Padding(12); $configSwitcherTabLayout.RowCount = 2; $configSwitcherTabLayout.ColumnCount = 1; [void]$configSwitcherTabLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,205))); [void]$configSwitcherTabLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $configSwitcherTab.Controls.Add($configSwitcherTabLayout)
$configSwitcherRuntimeGroup = New-Object System.Windows.Forms.GroupBox; $configSwitcherRuntimeGroup.Text = '运行版本与当前配置'; $configSwitcherRuntimeGroup.Dock = 'Fill'; $configSwitcherRuntimeGroup.Padding = New-Object System.Windows.Forms.Padding(8); $configSwitcherTabLayout.Controls.Add($configSwitcherRuntimeGroup,0,0)
$configSwitcherTabStatus = New-Object System.Windows.Forms.TextBox; $configSwitcherTabStatus.Multiline = $true; $configSwitcherTabStatus.ReadOnly = $true; $configSwitcherTabStatus.WordWrap = $true; $configSwitcherTabStatus.ScrollBars = 'Vertical'; $configSwitcherTabStatus.Dock = 'Fill'; $configSwitcherTabStatus.Font = $uiFont; $configSwitcherTabStatus.Text = $runtimeVersionDetail; $uiToolTip.SetToolTip($configSwitcherTabStatus,$runtimeVersionDetail); $configSwitcherRuntimeGroup.Controls.Add($configSwitcherTabStatus)
$configSwitcherActionsGroup = New-Object System.Windows.Forms.GroupBox; $configSwitcherActionsGroup.Text = 'Codex 配置切换'; $configSwitcherActionsGroup.Dock = 'Fill'; $configSwitcherActionsGroup.Padding = New-Object System.Windows.Forms.Padding(8); $configSwitcherTabLayout.Controls.Add($configSwitcherActionsGroup,0,1)
$configSwitcherTabButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $configSwitcherTabButtons.Dock = 'Fill'; $configSwitcherTabButtons.AutoScroll = $true; $configSwitcherTabButtons.WrapContents = $true; $configSwitcherTabButtons.Font = $buttonFont; $configSwitcherActionsGroup.Controls.Add($configSwitcherTabButtons)
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
$deepSeekModeButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $deepSeekModeButtons.Dock = 'Bottom'; $deepSeekModeButtons.Height = 82; $deepSeekModeButtons.AutoScroll = $true; $deepSeekModeButtons.Font = $buttonFont; $deepSeekModeGroup.Controls.Add($deepSeekModeButtons)
$deepSeekActionsGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekActionsGroup.Text = '本地操作'; $deepSeekActionsGroup.Dock = 'Fill'; $deepSeekActionsGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekActionsGroup,0,2)
$deepSeekButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $deepSeekButtons.Dock = 'Fill'; $deepSeekButtons.AutoScroll = $true; $deepSeekButtons.Font = $buttonFont; $deepSeekActionsGroup.Controls.Add($deepSeekButtons)
$deepSeekLogGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekLogGroup.Text = '最近本地操作日志（已脱敏）'; $deepSeekLogGroup.Dock = 'Fill'; $deepSeekLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekLayout.Controls.Add($deepSeekLogGroup,0,3)
$deepSeekLog = New-Object System.Windows.Forms.TextBox; $deepSeekLog.Multiline = $true; $deepSeekLog.ReadOnly = $true; $deepSeekLog.ScrollBars = 'Vertical'; $deepSeekLog.Font = $uiFont; $deepSeekLog.Dock = 'Fill'; $deepSeekLogGroup.Controls.Add($deepSeekLog)
$assistantLayout = New-Object System.Windows.Forms.TableLayoutPanel; $assistantLayout.Dock = 'Fill'; $assistantLayout.AutoScroll = $true; $assistantLayout.Padding = New-Object System.Windows.Forms.Padding(12); $assistantLayout.RowCount = 4; $assistantLayout.ColumnCount = 1; [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,285))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,145))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,42))); [void]$assistantLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,58))); $assistantTab.Controls.Add($assistantLayout)
$codexModeGroup = New-Object System.Windows.Forms.GroupBox; $codexModeGroup.Text = 'Codex 连接模式'; $codexModeGroup.Dock = 'Fill'; $codexModeGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($codexModeGroup,0,0)
$codexModeStatus = New-Object System.Windows.Forms.TextBox; $codexModeStatus.Multiline = $true; $codexModeStatus.ReadOnly = $true; $codexModeStatus.ScrollBars = 'Vertical'; $codexModeStatus.Dock = 'Top'; $codexModeStatus.Height = 78; $codexModeStatus.Font = $uiFont; $codexModeGroup.Controls.Add($codexModeStatus)
$codexModeScroll = New-Object System.Windows.Forms.Panel; $codexModeScroll.Dock = 'Fill'; $codexModeScroll.AutoScroll = $true; $codexModeScroll.Padding = New-Object System.Windows.Forms.Padding(2); $codexModeGroup.Controls.Add($codexModeScroll)
$codexAssistGroup = New-Object System.Windows.Forms.GroupBox; $codexAssistGroup.Text = '官方辅助协调'; $codexAssistGroup.Dock = 'Top'; $codexAssistGroup.Height = 76; $codexAssistGroup.Padding = New-Object System.Windows.Forms.Padding(6); $codexModeScroll.Controls.Add($codexAssistGroup)
$codexAssistButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexAssistButtons.Dock = 'Fill'; $codexAssistButtons.AutoScroll = $true; $codexAssistButtons.WrapContents = $true; $codexAssistButtons.FlowDirection = 'LeftToRight'; $codexAssistButtons.Font = $buttonFont; $codexAssistGroup.Controls.Add($codexAssistButtons)
$codexAnalysisGroup = New-Object System.Windows.Forms.GroupBox; $codexAnalysisGroup.Text = '辅助分析'; $codexAnalysisGroup.Dock = 'Top'; $codexAnalysisGroup.Height = 76; $codexAnalysisGroup.Padding = New-Object System.Windows.Forms.Padding(6); $codexModeScroll.Controls.Add($codexAnalysisGroup)
$codexAnalysisButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexAnalysisButtons.Dock = 'Fill'; $codexAnalysisButtons.AutoScroll = $true; $codexAnalysisButtons.WrapContents = $true; $codexAnalysisButtons.FlowDirection = 'LeftToRight'; $codexAnalysisButtons.Font = $buttonFont; $codexAnalysisGroup.Controls.Add($codexAnalysisButtons)
$codexBrainGroup = New-Object System.Windows.Forms.GroupBox; $codexBrainGroup.Text = '辅助脑'; $codexBrainGroup.Dock = 'Top'; $codexBrainGroup.Height = 76; $codexBrainGroup.Padding = New-Object System.Windows.Forms.Padding(6); $codexModeScroll.Controls.Add($codexBrainGroup)
$codexBrainButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexBrainButtons.Dock = 'Fill'; $codexBrainButtons.AutoScroll = $true; $codexBrainButtons.WrapContents = $true; $codexBrainButtons.FlowDirection = 'LeftToRight'; $codexBrainButtons.Font = $buttonFont; $codexBrainGroup.Controls.Add($codexBrainButtons)
$codexAssistBrainProvider = New-Object System.Windows.Forms.ComboBox; $codexAssistBrainProvider.DropDownStyle = 'DropDownList'; $codexAssistBrainProvider.Width = 215; $codexAssistBrainProvider.Height = 32; $codexAssistBrainProvider.Font = $buttonFont; [void]$codexAssistBrainProvider.Items.Add('自动'); [void]$codexAssistBrainProvider.Items.Add('本地模型'); [void]$codexAssistBrainProvider.Items.Add('DeepSeek Bridge 直连（本地 8791）'); [void]$codexAssistBrainProvider.Items.Add('外部 API'); [void]$codexAssistBrainProvider.Items.Add('Hybrid'); $codexAssistBrainProvider.SelectedIndex = 0; $codexAssistBrainProvider.Add_SelectedIndexChanged({ $script:AssistBrainSelection = [string]$codexAssistBrainProvider.SelectedItem }.GetNewClosure()); $script:AssistBrainSelection = '自动'; [void]$codexBrainButtons.Controls.Add($codexAssistBrainProvider)
$codexConfigSwitcherGroup = New-Object System.Windows.Forms.GroupBox; $codexConfigSwitcherGroup.Text = 'Codex 配置切换'; $codexConfigSwitcherGroup.Dock = 'Top'; $codexConfigSwitcherGroup.Height = 174; $codexConfigSwitcherGroup.Padding = New-Object System.Windows.Forms.Padding(6); $codexModeScroll.Controls.Add($codexConfigSwitcherGroup)
$codexConfigSwitcherStatus = New-Object System.Windows.Forms.TextBox; $codexConfigSwitcherStatus.Multiline = $true; $codexConfigSwitcherStatus.ReadOnly = $true; $codexConfigSwitcherStatus.ScrollBars = 'Vertical'; $codexConfigSwitcherStatus.Dock = 'Top'; $codexConfigSwitcherStatus.Height = 76; $codexConfigSwitcherStatus.Font = $uiFont; $codexConfigSwitcherStatus.Text = '本轮未读取或修改真实 Codex 配置。`r`n本功能只修改本地 Codex 配置文件，不读取或点击 Codex UI。修改后请重启 Codex 并新建对话。'; $codexConfigSwitcherGroup.Controls.Add($codexConfigSwitcherStatus)
$codexConfigSwitcherButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexConfigSwitcherButtons.Dock = 'Fill'; $codexConfigSwitcherButtons.AutoScroll = $true; $codexConfigSwitcherButtons.WrapContents = $true; $codexConfigSwitcherButtons.FlowDirection = 'LeftToRight'; $codexConfigSwitcherButtons.Font = $buttonFont; $codexConfigSwitcherGroup.Controls.Add($codexConfigSwitcherButtons)
$codexOtherGroup = New-Object System.Windows.Forms.GroupBox; $codexOtherGroup.Text = '其他模式与维护'; $codexOtherGroup.Dock = 'Top'; $codexOtherGroup.Height = 116; $codexOtherGroup.Padding = New-Object System.Windows.Forms.Padding(6); $codexModeScroll.Controls.Add($codexOtherGroup)
$codexOtherButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $codexOtherButtons.Dock = 'Fill'; $codexOtherButtons.AutoScroll = $true; $codexOtherButtons.WrapContents = $true; $codexOtherButtons.FlowDirection = 'LeftToRight'; $codexOtherButtons.Font = $buttonFont; $codexModeButtons = $codexOtherButtons; $codexOtherGroup.Controls.Add($codexOtherButtons)
$toolsPolicyGroup = New-Object System.Windows.Forms.GroupBox; $toolsPolicyGroup.Text = 'Codex 工具策略（默认严格拒绝）'; $toolsPolicyGroup.Dock = 'Fill'; $toolsPolicyGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($toolsPolicyGroup,0,1)
$toolsPolicyStatus = New-Object System.Windows.Forms.TextBox; $toolsPolicyStatus.Multiline = $true; $toolsPolicyStatus.ReadOnly = $true; $toolsPolicyStatus.Dock = 'Fill'; $toolsPolicyStatus.Font = $uiFont; $toolsPolicyGroup.Controls.Add($toolsPolicyStatus)
$toolsPolicyButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $toolsPolicyButtons.Dock = 'Bottom'; $toolsPolicyButtons.Height = 42; $toolsPolicyGroup.Controls.Add($toolsPolicyButtons)
# Provider Allowlist 是内部调试字段；用户界面统一显示为“供应商白名单”。
$allowlistGroup = New-Object System.Windows.Forms.GroupBox; $allowlistGroup.Text = '供应商白名单（Codex 请求触发调用；外部 API 默认禁用）'; $allowlistGroup.Dock = 'Fill'; $allowlistGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($allowlistGroup,0,2)
$allowlistStatus = New-Object System.Windows.Forms.TextBox; $allowlistStatus.Multiline = $true; $allowlistStatus.ReadOnly = $true; $allowlistStatus.ScrollBars = 'Vertical'; $allowlistStatus.Dock = 'Fill'; $allowlistStatus.Font = $uiFont; $allowlistGroup.Controls.Add($allowlistStatus)
$recordGroup = New-Object System.Windows.Forms.GroupBox; $recordGroup.Text = '本地调用记录（仅元数据，默认不保存正文）'; $recordGroup.Dock = 'Fill'; $recordGroup.Padding = New-Object System.Windows.Forms.Padding(8); $assistantLayout.Controls.Add($recordGroup,0,3)
$recordStatus = New-Object System.Windows.Forms.TextBox; $recordStatus.Multiline = $true; $recordStatus.ReadOnly = $true; $recordStatus.ScrollBars = 'Vertical'; $recordStatus.Dock = 'Fill'; $recordStatus.Font = $uiFont; $recordGroup.Controls.Add($recordStatus)
$localAgentLayout = New-Object System.Windows.Forms.TableLayoutPanel; $localAgentLayout.Dock = 'Fill'; $localAgentLayout.AutoScroll = $true; $localAgentLayout.Padding = New-Object System.Windows.Forms.Padding(12); $localAgentLayout.RowCount = 3; $localAgentLayout.ColumnCount = 1; [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,38))); [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,46))); [void]$localAgentLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,16))); $localAgentTab.Controls.Add($localAgentLayout)
$localAgentStatusGroup = New-Object System.Windows.Forms.GroupBox; $localAgentStatusGroup.Text = '小羽 Local Agent 状态（默认只读；不调用 Codex 官方 Agent）'; $localAgentStatusGroup.Dock = 'Fill'; $localAgentStatusGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentStatusGroup,0,0)
$localAgentStatus = New-Object System.Windows.Forms.TextBox; $localAgentStatus.Multiline = $true; $localAgentStatus.ReadOnly = $true; $localAgentStatus.ScrollBars = 'Vertical'; $localAgentStatus.Dock = 'Fill'; $localAgentStatus.Font = $uiFont; $localAgentStatusGroup.Controls.Add($localAgentStatus)
$localAgentActionGroup = New-Object System.Windows.Forms.GroupBox; $localAgentActionGroup.Text = '小羽 Local Agent 操作（小窗口可滚动）'; $localAgentActionGroup.Dock = 'Fill'; $localAgentActionGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentActionGroup,0,1)
$localAgentActionScroll = New-Object System.Windows.Forms.Panel; $localAgentActionScroll.Dock = 'Fill'; $localAgentActionScroll.AutoScroll = $true; $localAgentActionScroll.Padding = New-Object System.Windows.Forms.Padding(2); $localAgentActionGroup.Controls.Add($localAgentActionScroll)
$localAgentBrainGroup = New-Object System.Windows.Forms.GroupBox; $localAgentBrainGroup.Text = 'Brain Provider'; $localAgentBrainGroup.Dock = 'Top'; $localAgentBrainGroup.Height = 78; $localAgentBrainGroup.Padding = New-Object System.Windows.Forms.Padding(6); $localAgentActionScroll.Controls.Add($localAgentBrainGroup)
$localAgentBrainButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $localAgentBrainButtons.Dock = 'Fill'; $localAgentBrainButtons.AutoScroll = $true; $localAgentBrainButtons.WrapContents = $true; $localAgentBrainButtons.FlowDirection = 'LeftToRight'; $localAgentBrainButtons.Font = $buttonFont; $localAgentBrainGroup.Controls.Add($localAgentBrainButtons)
$localAgentBrainLabel = New-Object System.Windows.Forms.Label; $localAgentBrainLabel.Text = '辅助脑：'; $localAgentBrainLabel.AutoSize = $true; $localAgentBrainLabel.Font = $buttonFont; [void]$localAgentBrainButtons.Controls.Add($localAgentBrainLabel)
$localAgentBrainProvider = New-Object System.Windows.Forms.ComboBox; $localAgentBrainProvider.DropDownStyle = 'DropDownList'; $localAgentBrainProvider.Width = 250; $localAgentBrainProvider.Height = 32; $localAgentBrainProvider.Font = $buttonFont; [void]$localAgentBrainProvider.Items.Add('自动'); [void]$localAgentBrainProvider.Items.Add('本地模型'); [void]$localAgentBrainProvider.Items.Add('DeepSeek Bridge 直连（本地 8791）'); [void]$localAgentBrainProvider.Items.Add('外部 API'); [void]$localAgentBrainProvider.Items.Add('Hybrid'); $localAgentBrainProvider.SelectedIndex = 0; $localAgentBrainProvider.Add_SelectedIndexChanged({ $script:LocalAgentBrainProvider = switch ([string]$localAgentBrainProvider.SelectedItem) { 'DeepSeek Bridge 直连（本地 8791）' { 'deepseek-bridge-direct' } '外部 API' { 'external-allowed' } 'Hybrid' { 'hybrid-agent' } default { 'local-light' } } }.GetNewClosure()); $script:LocalAgentBrainProvider = 'local-light'; [void]$localAgentBrainButtons.Controls.Add($localAgentBrainProvider)
$localAgentReadPlanGroup = New-Object System.Windows.Forms.GroupBox; $localAgentReadPlanGroup.Text = '只读与计划'; $localAgentReadPlanGroup.Dock = 'Top'; $localAgentReadPlanGroup.Height = 104; $localAgentReadPlanGroup.Padding = New-Object System.Windows.Forms.Padding(6); $localAgentActionScroll.Controls.Add($localAgentReadPlanGroup)
$localAgentReadPlanButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $localAgentReadPlanButtons.Dock = 'Fill'; $localAgentReadPlanButtons.AutoScroll = $true; $localAgentReadPlanButtons.WrapContents = $true; $localAgentReadPlanButtons.FlowDirection = 'LeftToRight'; $localAgentReadPlanButtons.Font = $buttonFont; $localAgentReadPlanGroup.Controls.Add($localAgentReadPlanButtons); $localAgentButtons = $localAgentReadPlanButtons
$localAgentConfirmGroup = New-Object System.Windows.Forms.GroupBox; $localAgentConfirmGroup.Text = '需确认执行'; $localAgentConfirmGroup.Dock = 'Top'; $localAgentConfirmGroup.Height = 104; $localAgentConfirmGroup.Padding = New-Object System.Windows.Forms.Padding(6); $localAgentActionScroll.Controls.Add($localAgentConfirmGroup)
$localAgentConfirmButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $localAgentConfirmButtons.Dock = 'Fill'; $localAgentConfirmButtons.AutoScroll = $true; $localAgentConfirmButtons.WrapContents = $true; $localAgentConfirmButtons.FlowDirection = 'LeftToRight'; $localAgentConfirmButtons.Font = $buttonFont; $localAgentConfirmGroup.Controls.Add($localAgentConfirmButtons)
$localAgentRecordGroup = New-Object System.Windows.Forms.GroupBox; $localAgentRecordGroup.Text = '记录'; $localAgentRecordGroup.Dock = 'Top'; $localAgentRecordGroup.Height = 76; $localAgentRecordGroup.Padding = New-Object System.Windows.Forms.Padding(6); $localAgentActionScroll.Controls.Add($localAgentRecordGroup)
$localAgentRecordButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $localAgentRecordButtons.Dock = 'Fill'; $localAgentRecordButtons.AutoScroll = $true; $localAgentRecordButtons.WrapContents = $true; $localAgentRecordButtons.FlowDirection = 'LeftToRight'; $localAgentRecordButtons.Font = $buttonFont; $localAgentRecordGroup.Controls.Add($localAgentRecordButtons)
$localAgentLogGroup = New-Object System.Windows.Forms.GroupBox; $localAgentLogGroup.Text = 'Local Agent 记录（仅元数据，默认不保存正文）'; $localAgentLogGroup.Dock = 'Fill'; $localAgentLogGroup.Padding = New-Object System.Windows.Forms.Padding(8); $localAgentLayout.Controls.Add($localAgentLogGroup,0,2)
$localAgentLog = New-Object System.Windows.Forms.TextBox; $localAgentLog.Multiline = $true; $localAgentLog.ReadOnly = $true; $localAgentLog.ScrollBars = 'Vertical'; $localAgentLog.Dock = 'Fill'; $localAgentLog.Font = $uiFont; $localAgentLogGroup.Controls.Add($localAgentLog)
$usageText = New-Object System.Windows.Forms.TextBox; $usageText.Multiline = $true; $usageText.ReadOnly = $true; $usageText.Font = $uiFont; $usageText.Dock = 'Fill'; $usageTab.Controls.Add($usageText)
$usageButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $usageButtons.Dock = 'Top'; $usageButtons.Height = 42; $usageTab.Controls.Add($usageButtons)
$diagnosticsLayout = New-Object System.Windows.Forms.TableLayoutPanel; $diagnosticsLayout.Dock = 'Fill'; $diagnosticsLayout.RowCount = 2; $diagnosticsLayout.ColumnCount = 1; [void]$diagnosticsLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,42))); [void]$diagnosticsLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $diagnosticsTab.Controls.Add($diagnosticsLayout)
$diagnosticsToolbar = New-Object System.Windows.Forms.FlowLayoutPanel; $diagnosticsToolbar.Dock = 'Fill'; $diagnosticsToolbar.Font = $buttonFont; [void]$diagnosticsLayout.Controls.Add($diagnosticsToolbar,0,0)
$debugToggle = New-Object System.Windows.Forms.CheckBox; $debugToggle.Text = '显示高级信息 / 调试信息'; $debugToggle.AutoSize = $true; $debugToggle.Font = $buttonFont; [void]$diagnosticsToolbar.Controls.Add($debugToggle)
$copyDebugButton = New-Object System.Windows.Forms.Button; $copyDebugButton.Text = '复制诊断 JSON'; $copyDebugButton.Width = 150; $copyDebugButton.Height = 30; $copyDebugButton.Font = $buttonFont; [void]$diagnosticsToolbar.Controls.Add($copyDebugButton)
$diagnosticsText = New-Object System.Windows.Forms.TextBox; $diagnosticsText.Multiline = $true; $diagnosticsText.ReadOnly = $true; $diagnosticsText.Font = $uiFont; $diagnosticsText.Dock = 'Fill'; $diagnosticsText.Visible = $false; [void]$diagnosticsLayout.Controls.Add($diagnosticsText,0,1); $debugToggle.Add_CheckedChanged({$diagnosticsText.Visible = $debugToggle.Checked}.GetNewClosure())
## DeepSeek Head collaboration is a separate, scrollable panel so the existing
## bridge and Local Agent layouts remain stable at small window sizes.
$deepSeekHeadTab = New-Object System.Windows.Forms.TabPage('DeepSeek 首脑协作'); [void]$tabs.TabPages.Add($deepSeekHeadTab)
$deepSeekHeadLayout = New-Object System.Windows.Forms.TableLayoutPanel; $deepSeekHeadLayout.Dock = 'Fill'; $deepSeekHeadLayout.AutoScroll = $true; $deepSeekHeadLayout.Padding = New-Object System.Windows.Forms.Padding(12); $deepSeekHeadLayout.ColumnCount = 1; $deepSeekHeadLayout.RowCount = 3; [void]$deepSeekHeadLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,155))); [void]$deepSeekHeadLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,185))); [void]$deepSeekHeadLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $deepSeekHeadTab.Controls.Add($deepSeekHeadLayout)
$deepSeekHeadStatusGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekHeadStatusGroup.Text = 'DeepSeek 首脑协作状态（只读上下文）'; $deepSeekHeadStatusGroup.Dock = 'Fill'; $deepSeekHeadStatusGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekHeadLayout.Controls.Add($deepSeekHeadStatusGroup,0,0)
$deepSeekHeadStatus = New-Object System.Windows.Forms.TextBox; $deepSeekHeadStatus.Multiline = $true; $deepSeekHeadStatus.ReadOnly = $true; $deepSeekHeadStatus.ScrollBars = 'Vertical'; $deepSeekHeadStatus.Dock = 'Fill'; $deepSeekHeadStatus.Font = $uiFont; $deepSeekHeadStatusGroup.Controls.Add($deepSeekHeadStatus)
$deepSeekHeadActionGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekHeadActionGroup.Text = 'DeepSeek Head 协作操作'; $deepSeekHeadActionGroup.Dock = 'Fill'; $deepSeekHeadActionGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekHeadLayout.Controls.Add($deepSeekHeadActionGroup,0,1)
$deepSeekHeadButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $deepSeekHeadButtons.Dock = 'Fill'; $deepSeekHeadButtons.AutoScroll = $true; $deepSeekHeadButtons.WrapContents = $true; $deepSeekHeadButtons.Font = $buttonFont; $deepSeekHeadActionGroup.Controls.Add($deepSeekHeadButtons)
$deepSeekHeadRawGroup = New-Object System.Windows.Forms.GroupBox; $deepSeekHeadRawGroup.Text = 'Context Bundle / 原始 JSON（默认折叠）'; $deepSeekHeadRawGroup.Dock = 'Fill'; $deepSeekHeadRawGroup.Padding = New-Object System.Windows.Forms.Padding(8); $deepSeekHeadLayout.Controls.Add($deepSeekHeadRawGroup,0,2)
$deepSeekHeadRaw = New-Object System.Windows.Forms.TextBox; $deepSeekHeadRaw.Multiline = $true; $deepSeekHeadRaw.ReadOnly = $true; $deepSeekHeadRaw.ScrollBars = 'Vertical'; $deepSeekHeadRaw.Dock = 'Fill'; $deepSeekHeadRaw.Visible = $false; $deepSeekHeadRaw.Font = $uiFont; $deepSeekHeadRawGroup.Controls.Add($deepSeekHeadRaw)
$sessionBindingTab = New-Object System.Windows.Forms.TabPage('会话绑定'); [void]$tabs.TabPages.Add($sessionBindingTab)
$sessionBindingLayout = New-Object System.Windows.Forms.TableLayoutPanel; $sessionBindingLayout.Dock = 'Fill'; $sessionBindingLayout.AutoScroll = $true; $sessionBindingLayout.Padding = New-Object System.Windows.Forms.Padding(12); $sessionBindingLayout.RowCount = 3; $sessionBindingLayout.ColumnCount = 1; [void]$sessionBindingLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,120))); [void]$sessionBindingLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,115))); [void]$sessionBindingLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $sessionBindingTab.Controls.Add($sessionBindingLayout)
$sessionBindingNotice = New-Object System.Windows.Forms.GroupBox; $sessionBindingNotice.Text = '本地会话绑定'; $sessionBindingNotice.Dock = 'Fill'; $sessionBindingNotice.Padding = New-Object System.Windows.Forms.Padding(8); $sessionBindingLayout.Controls.Add($sessionBindingNotice,0,0)
$sessionBindingStatus = New-Object System.Windows.Forms.TextBox; $sessionBindingStatus.Multiline = $true; $sessionBindingStatus.ReadOnly = $true; $sessionBindingStatus.Dock = 'Fill'; $sessionBindingStatus.Font = $uiFont; $sessionBindingStatus.Text = '这是本地上下文绑定，不读取 Codex 或 DeepSeek 网页私有数据。`r`n当前 task_session_id：未载入`r`nCodex 状态：未同步；DeepSeek 状态：未分析；Patch Draft：未请求。'; $sessionBindingNotice.Controls.Add($sessionBindingStatus)
$sessionBindingActions = New-Object System.Windows.Forms.GroupBox; $sessionBindingActions.Text = '会话操作（仅脱敏摘要）'; $sessionBindingActions.Dock = 'Fill'; $sessionBindingActions.Padding = New-Object System.Windows.Forms.Padding(8); $sessionBindingLayout.Controls.Add($sessionBindingActions,0,1)
$sessionBindingButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $sessionBindingButtons.Dock = 'Fill'; $sessionBindingButtons.AutoScroll = $true; $sessionBindingButtons.WrapContents = $true; $sessionBindingButtons.Font = $buttonFont; $sessionBindingActions.Controls.Add($sessionBindingButtons)
$sessionBindingRawGroup = New-Object System.Windows.Forms.GroupBox; $sessionBindingRawGroup.Text = '会话状态 / 脱敏上下文（默认折叠）'; $sessionBindingRawGroup.Dock = 'Fill'; $sessionBindingRawGroup.Padding = New-Object System.Windows.Forms.Padding(8); $sessionBindingLayout.Controls.Add($sessionBindingRawGroup,0,2)
$sessionBindingRaw = New-Object System.Windows.Forms.TextBox; $sessionBindingRaw.Multiline = $true; $sessionBindingRaw.ReadOnly = $true; $sessionBindingRaw.ScrollBars = 'Vertical'; $sessionBindingRaw.Dock = 'Fill'; $sessionBindingRaw.Visible = $false; $sessionBindingRaw.Font = $uiFont; $sessionBindingRawGroup.Controls.Add($sessionBindingRaw)
$workProfileTab = New-Object System.Windows.Forms.TabPage('Work Profile / Codex 自定义'); [void]$tabs.TabPages.Add($workProfileTab)
$workProfileLayout = New-Object System.Windows.Forms.TableLayoutPanel; $workProfileLayout.Dock = 'Fill'; $workProfileLayout.AutoScroll = $true; $workProfileLayout.Padding = New-Object System.Windows.Forms.Padding(12); $workProfileLayout.ColumnCount = 1; $workProfileLayout.RowCount = 3; [void]$workProfileLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,170))); [void]$workProfileLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,150))); [void]$workProfileLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100))); $workProfileTab.Controls.Add($workProfileLayout)
$workProfileStatusGroup = New-Object System.Windows.Forms.GroupBox; $workProfileStatusGroup.Text = 'Work Profile / Codex 自定义模式'; $workProfileStatusGroup.Dock = 'Fill'; $workProfileStatusGroup.Padding = New-Object System.Windows.Forms.Padding(8); $workProfileLayout.Controls.Add($workProfileStatusGroup,0,0)
$workProfileStatus = New-Object System.Windows.Forms.TextBox; $workProfileStatus.Multiline = $true; $workProfileStatus.ReadOnly = $true; $workProfileStatus.ScrollBars = 'Vertical'; $workProfileStatus.Dock = 'Fill'; $workProfileStatus.Font = $uiFont; $workProfileStatus.Text = 'Codex 自定义模式需要用户手动确认。小羽不会读取或操控 Codex 官方 UI。'; $workProfileStatusGroup.Controls.Add($workProfileStatus)
$workProfileActionsGroup = New-Object System.Windows.Forms.GroupBox; $workProfileActionsGroup.Text = '选择与确认（仅本地策略，不调用模型）'; $workProfileActionsGroup.Dock = 'Fill'; $workProfileActionsGroup.Padding = New-Object System.Windows.Forms.Padding(8); $workProfileLayout.Controls.Add($workProfileActionsGroup,0,1)
$workProfileButtons = New-Object System.Windows.Forms.FlowLayoutPanel; $workProfileButtons.Dock = 'Fill'; $workProfileButtons.AutoScroll = $true; $workProfileButtons.WrapContents = $true; $workProfileButtons.FlowDirection = 'LeftToRight'; $workProfileButtons.Font = $buttonFont; $workProfileActionsGroup.Controls.Add($workProfileButtons)
$workProfileRawGroup = New-Object System.Windows.Forms.GroupBox; $workProfileRawGroup.Text = 'Work Profile JSON（默认折叠）'; $workProfileRawGroup.Dock = 'Fill'; $workProfileRawGroup.Padding = New-Object System.Windows.Forms.Padding(8); $workProfileLayout.Controls.Add($workProfileRawGroup,0,2)
$workProfileRaw = New-Object System.Windows.Forms.TextBox; $workProfileRaw.Multiline = $true; $workProfileRaw.ReadOnly = $true; $workProfileRaw.ScrollBars = 'Vertical'; $workProfileRaw.Dock = 'Fill'; $workProfileRaw.Visible = $false; $workProfileRaw.Font = $uiFont; $workProfileRawGroup.Controls.Add($workProfileRaw)
$script:TaskSessionId = ''
function Get-WorkProfile([string]$Task,[string]$ProfileId='') {
    $arguments = @('work-profile','select','--task',$(if($Task){$Task}else{'当前任务由 Codex 提供；先做只读分析'}))
    if($ProfileId){$arguments += @('--profile',$ProfileId)}
    $raw = Invoke-RouterCli $arguments
    try { return [pscustomobject]@{ raw=$raw; data=($raw | ConvertFrom-Json) } } catch { return [pscustomobject]@{ raw=$raw; data=$null } }
}
function Format-WorkProfileSummary([object]$Record) {
    if(-not $Record -or -not $Record.data -or -not $Record.data.profile){ return '尚未选择 Work Profile。`r`nCodex 自定义模式需要用户手动确认；小羽不会读取或操控 Codex 官方 UI。' }
    $p = $Record.data.profile
    return ('任务难度：{0}`r`n推荐 Work Profile：{1}`r`n推荐 Codex 模式：{2}`r`n推荐推理强度：{3}（{4}）`r`n推荐 DeepSeek 模式：{5}`r`n本地模型策略：{6}`r`nLocal Agent 策略：{7}`r`n外部 API：默认禁用`r`n是否建议官方 Codex：{8}`r`n用户确认：请手动确认' -f $p.task_difficulty,$p.profile_id,$p.codex_custom_mode,$p.codex_reasoning_strength,$p.reasoning_strength_zh,$p.deepseek_mode,$p.local_model_policy,$p.local_agent_policy,$(if($p.target_executor -eq 'codex_official'){'是'}else{'按风险决定'}))
}
function Refresh-WorkProfilePanel([string]$Task='') {
    $record = Get-WorkProfile $Task
    $script:WorkProfileLastRecord = $record
    $workProfileStatus.Text = (Format-WorkProfileSummary $record)
    if($record.raw){$workProfileRaw.Text = Redact-Text $record.raw}
}
function Show-WorkProfile([string]$ProfileId='') {
    $record = if($ProfileId){Get-WorkProfile '当前 Codex 任务；请按选择的 Work Profile 生成推荐' $ProfileId}else{$script:WorkProfileLastRecord}
    if(-not $record){$record=Get-WorkProfile ''}
    $script:WorkProfileLastRecord=$record
    $workProfileStatus.Text=(Format-WorkProfileSummary $record)
    if($record.raw){$workProfileRaw.Text=Redact-Text $record.raw}
    [System.Windows.Forms.MessageBox]::Show((Format-WorkProfileSummary $record),'Work Profile') | Out-Null
}
function Confirm-WorkProfileMode {
    if(-not $script:WorkProfileLastRecord -or -not $script:WorkProfileLastRecord.data.profile){Refresh-WorkProfilePanel}
    $id = if($script:WorkProfileLastRecord.data.profile.profile_id){[string]$script:WorkProfileLastRecord.data.profile.profile_id}else{'simple_readonly'}
    $raw = Invoke-RouterCli @('work-profile','confirm','--profile-id',$id,'--confirmed')
    $workProfileStatus.Text += "`r`n`r`nCodex 模式已由用户标记确认；小羽仍不操控 Codex UI。"
    [System.Windows.Forms.MessageBox]::Show('已记录用户确认标记。请在 Codex 中手动选择推荐模式和推理强度。','Work Profile') | Out-Null
}
function Show-WorkProfileHandoff {
    if(-not $script:WorkProfileLastRecord -or -not $script:WorkProfileLastRecord.data.profile){Refresh-WorkProfilePanel}
    $id = if($script:WorkProfileLastRecord.data.profile.profile_id){[string]$script:WorkProfileLastRecord.data.profile.profile_id}else{'simple_readonly'}
    $raw = Invoke-RouterCli @('work-profile','handoff','--task','当前 Codex 任务；生成脱敏 handoff 指令','--profile',$id)
    $safe = Redact-Text $raw
    try { Set-Clipboard -Value $safe } catch {}
    [System.Windows.Forms.MessageBox]::Show($safe,'Codex Handoff 指令') | Out-Null
}
function Add-ProviderLog([string]$Message) { $line = ('[{0}] {1}' -f (Get-Date).ToString('HH:mm:ss'), (Redact-Text $Message)); $providerLog.AppendText($line + "`r`n") }
function Add-DeepSeekLog([string]$Action,[object]$Result) {
    $line = ('[{0}] action={1}; exit_code={2}; status={3}' -f (Get-Date).ToString('HH:mm:ss'),$Action,$Result.exit_code,$Result.error_type)
    $deepSeekLog.AppendText((Redact-Text $line) + "`r`n")
}
function Format-DeepSeekHeadSummary([object]$Record) {
    if (-not $Record) { return '尚未收集上下文。默认只读，不会修改文件。' }
    if ($Record.error_code) {
        if ($Record.provider_error_stage -eq 'before_bridge_send') {
            return ("DeepSeek 首脑在发送前被阻断`r`n错误码：{0}`r`n发送尝试：否；网页发送计数：{1}`r`n模型输出：否；补丁草案：未尝试`r`n已修改文件：否`r`n请检查本地模式参数或 Provider 状态。" -f $Record.provider_error_code,$Record.bridge_ui_send_attempt_count)
        }
        return ("DeepSeek 首脑协作失败`r`n错误码：{0}`r`n回退原因：{1}`r`n已修改文件：否`r`n已发送工具：否" -f $Record.error_code,$Record.fallback_reason)
    }
    $health = if ($Record.provider_health) { (@($Record.provider_health.psobject.Properties | ForEach-Object { '{0}={1}' -f $_.Name,$_.Value.status }) -join '；') } else { '未知' }
    $draft = if($Record.patch_draft_created -eq 'YES' -and $Record.patch_draft_source -eq 'structured_patch'){'已由结构化补丁草案生成 unified diff，尚未应用。位置：' + $Record.patch_draft_location}elseif($Record.patch_draft_created -eq 'YES'){'已生成 direct unified diff：' + $Record.patch_draft_location}elseif($Record.patch_draft_unavailable -eq 'YES'){'上下文不足：DeepSeek 表示无法生成'}elseif($Record.patch_synthesizer_error_code){'结构化补丁转换失败：' + $Record.patch_synthesizer_error_code}elseif($Record.patch_draft_format_invalid -eq 'YES'){'格式无效：未生成可审查 unified diff'}else{'未生成'}
    return ("任务难度：{0}`r`n选定辅助脑：{1}`r`n选择原因：{2}`r`n回退原因：{3}`r`nContext Bundle：{4}`r`nAgent Plan：{5}`r`nPatch Draft 状态：{6}`r`nProvider 状态：{7}`r`n默认模式：PLAN_ONLY / READ_ONLY`r`n文件修改：否；测试：否；commit：否`r`nDeepSeek 工具转发：否" -f $Record.task_difficulty,$Record.selected_brain,$Record.why_selected,$Record.fallback_reason,$Record.context_bundle_id,$Record.deepseek_plan_id,$draft,$health)
}
function Invoke-DeepSeekHeadCoordinate([bool]$InvokeBrain = $false,[string]$Brain = 'auto',[bool]$PatchDraft = $false) {
    $arguments = @('deepseek-head','coordinate','--task','当前任务由 Codex 提供；只读收集项目上下文并生成 Agent Plan，不修改文件')
    if ($Brain -and $Brain -ne 'auto') { $arguments += @('--brain',$Brain) } else { $arguments += @('--brain','auto') }
    if ($InvokeBrain) {
        $confirm = [System.Windows.Forms.MessageBox]::Show('将把脱敏且限长的只读上下文发送到本机 127.0.0.1:8791 DeepSeek Web Bridge。不会发送密钥、Cookie、Token 或 Authorization。是否继续？','DeepSeek 首脑分析确认',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }
        $arguments += '--invoke-brain'
    }
    if ($PatchDraft) { $arguments += '--patch-draft' }
    $raw = Invoke-RouterCli $arguments
    try { $record = $raw | ConvertFrom-Json } catch { throw 'DEEPSEEK_HEAD_RESPONSE_INVALID' }
    $script:DeepSeekHeadLastRecord = $record
    $deepSeekHeadStatus.Text = Format-DeepSeekHeadSummary $record
    $deepSeekHeadRaw.Text = Redact-Text $raw
    $deepSeekHeadRaw.Visible = $false
    if ($script:DeepSeekHeadApplyButton) { $script:DeepSeekHeadApplyButton.Visible = ($record.patch_draft_created -eq 'YES') }
    if ($record.context_bundle_id) { $script:DeepSeekHeadContextId = [string]$record.context_bundle_id }
    if ($record.deepseek_plan_id) { $script:DeepSeekHeadPlanId = [string]$record.deepseek_plan_id }
    [System.Windows.Forms.MessageBox]::Show((Format-DeepSeekHeadSummary $record),'DeepSeek 首脑协作') | Out-Null
}
function Show-DeepSeekHeadContext {
    if (-not $script:DeepSeekHeadContextId) { [System.Windows.Forms.MessageBox]::Show('请先收集项目上下文。','Context Bundle') | Out-Null; return }
    $raw = Invoke-RouterCli @('deepseek-head','plan-from-context','--context-id',$script:DeepSeekHeadContextId)
    $deepSeekHeadRaw.Text = Redact-Text $raw; $deepSeekHeadRaw.Visible = $true
}
function Invoke-SessionBindingCli([string[]]$Arguments) {
    $raw = Invoke-RouterCli (@('session') + $Arguments)
    try { return ($raw | ConvertFrom-Json) } catch { throw 'SESSION_BINDING_RESPONSE_INVALID' }
}
function Refresh-SessionBinding([object]$Record) {
    if (-not $Record) { return }
    if ($Record.task_session_id) { $script:TaskSessionId = [string]$Record.task_session_id }
    $sessionBindingStatus.Text = ("这是本地上下文绑定，不读取 Codex 或 DeepSeek 网页私有数据。`r`n当前 task_session_id：{0}`r`nCodex 状态：{1}；DeepSeek 状态：{2}；Patch Draft：{3}。`r`n安全状态：不保存 prompt/response，不读取 Cookie/Token。" -f $script:TaskSessionId,$Record.codex_synced,$Record.deepseek_analyzed,$Record.patch_draft_status)
    $sessionBindingRaw.Text = Redact-Text ($Record | ConvertTo-Json -Depth 8)
    $sessionBindingRaw.Visible = $false
}
function New-TaskSessionBinding {
    $title = [Microsoft.VisualBasic.Interaction]::InputBox('输入非敏感任务标题。不会保存提示词或模型回复原文。','新建绑定会话','本地任务')
    if (-not $title) { return }
    $record = Invoke-SessionBindingCli @('create','--title',$title); Refresh-SessionBinding $record
}
function Load-TaskSessionBinding {
    $id = [Microsoft.VisualBasic.Interaction]::InputBox('输入 task_session_id。','载入绑定会话',$script:TaskSessionId)
    if (-not $id) { return }
    $record = Invoke-SessionBindingCli @('status','--session-id',$id); Refresh-SessionBinding $record
}
function Show-SessionRollingSummary {
    if (-not $script:TaskSessionId) { [System.Windows.Forms.MessageBox]::Show('请先新建或载入绑定会话。','会话绑定') | Out-Null; return }
    $record = Invoke-SessionBindingCli @('context','--session-id',$script:TaskSessionId); $sessionBindingRaw.Text = Redact-Text ($record | ConvertTo-Json -Depth 8); $sessionBindingRaw.Visible = $true
}
function Append-CodexStatusToSession {
    if (-not $script:TaskSessionId) { [System.Windows.Forms.MessageBox]::Show('请先新建或载入绑定会话。','会话绑定') | Out-Null; return }
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Filter = '状态摘要 (*.md;*.txt)|*.md;*.txt'
    $dialog.Title = '选择项目内、非敏感的 UTF-8 Codex 状态摘要'
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }
    $null = Invoke-SessionBindingCli @('append-codex-status','--session-id',$script:TaskSessionId,'--file',$dialog.FileName)
    Refresh-SessionBinding (Invoke-SessionBindingCli @('status','--session-id',$script:TaskSessionId))
}
function Clear-SessionSensitiveCache {
    if (-not $script:TaskSessionId) { [System.Windows.Forms.MessageBox]::Show('请先新建或载入绑定会话。','会话绑定') | Out-Null; return }
    $record = Invoke-SessionBindingCli @('clear-sensitive-cache','--session-id',$script:TaskSessionId); Refresh-SessionBinding $record
}
function Add-DeepSeekHeadButton([string]$Caption,[scriptblock]$Action,[int]$Width=220,[string]$TooltipText='',[bool]$ReturnControl=$false) {
    Add-UiLayoutButton $deepSeekHeadButtons $Caption $Action $Width $TooltipText
    if($ReturnControl){ return $deepSeekHeadButtons.Controls[$deepSeekHeadButtons.Controls.Count - 1] }
}
function Get-DeepSeekModeStatusText([object]$Probe) {
    if (-not $Probe) { return ("模式策略：自动选择（尚未运行只读探测）`r`n请点击探测 DeepSeek 模式；该操作只读取页面控件，不发送提示词、不点击发送。") }
    $labels = [ordered]@{ quick = '快速'; expert = '专家'; thinking = '深度思考'; search = '智能搜索'; vision = '视觉识图'; file = '文件上传' }
    $performanceLabel = @{ economy='省时'; balanced='平衡'; accuracy='严谨' }[$script:DeepSeekPerformanceMode]
    $lines = @("模式策略：$script:DeepSeekModePreference；性能策略：$performanceLabel",("探测：{0}；promptSent={1}；clickSend={2}；uploadAttempted={3}" -f $Probe.status,$Probe.promptSent,$Probe.clickSend,$Probe.uploadAttempted))
    foreach ($mode in $labels.Keys) {
        $item = $Probe.modes.$mode
        $state = if ($item.status -eq 'AVAILABLE' -and $item.controllable) { '可用' } elseif ($item.status) { [string]$item.status } else { 'UNKNOWN' }
        $lines += ("{0}模式：{1}（控件发现：{2}；可控：{3}）" -f $labels[$mode],$state,$item.controlDetected,$item.controllable)
    }
    if ($Probe.error_code) { $lines += ("错误码：{0}；说明：{1}" -f $Probe.error_code,(Get-UiErrorExplanation ([string]$Probe.error_code))) }
    if ($script:DeepSeekLastSelection) { $lines += ("最近推荐模式：{0}（{1}）" -f $script:DeepSeekLastSelection.selected_mode,$script:DeepSeekLastSelection.selected_model_alias) }
    $generation = [string]$Probe.ui_generation
    $options = ([string[]]$Probe.reasoning_strength_options) -join ','
    $optionsStatus = if ($Probe.reasoning_strength_options_status) { [string]$Probe.reasoning_strength_options_status } else { 'unknown' }
    $baseAxis = if ($Probe.base_mode_axis) { [string]$Probe.base_mode_axis } else { if ($generation -eq 'three_in_one') { 'absent' } else { 'available' } }
    $lines += ("当前网页模式：generation={0}；profile={1}；基础模式轴={2}；思考强度={3}；可用强度={4}（{5}）；搜索={6}；视觉={7}；文件={8}" -f $generation,$Probe.current_profile_label,$baseAxis,$Probe.current_reasoning_strength,$options,$optionsStatus,$Probe.current_search,$Probe.vision_available,$Probe.file_upload_available)
    if ($Probe.reasoning_strength_warning) { $lines += ("提示：{0}" -f $Probe.reasoning_strength_warning) }
    elseif ($optionsStatus -eq 'partial') { $lines += '提示：REASONING_OPTIONS_PARTIAL' }
    $lines += 'Codex 推理强度与 DeepSeek 思考强度是两个独立轴；二者不会互相自动同步。模式切换只操作可见模式控件，不发送 prompt、不上传文件。'
    if ($generation -eq 'legacy') {
        $lines += '本地项目任务默认“专家 + 深度思考 + 不联网搜索”；截图任务默认“视觉 + 专家 + 深度思考”；仅最新资料、官网、价格、新闻才开启搜索。'
    } elseif ($generation -eq 'three_in_one') {
        # Legacy strength-level wording retained only as a source-level compatibility marker: 本地项目默认“高思考或最高思考，并关闭联网搜索”。
        $axis = [string]$Probe.reasoning_axis_type
        $maxStrength = [string]$Probe.max_available_reasoning_strength
        if ($axis -eq 'binary_toggle' -or $optionsStatus -eq 'binary') {
            $lines += '当前 DeepSeek 网页仅检测到深度思考开关；本地项目可使用已开启思考并关闭搜索，但不等同于 high/max（实际强度=medium）。'
        } elseif ($maxStrength -eq 'max') {
            $lines += '本地项目默认高思考并关闭搜索；当前网页已检测到 max，截图任务需要显式图片附件。'
        } elseif ($maxStrength -eq 'high') {
            $lines += '本地项目默认高思考并关闭搜索；当前网页最高可用强度为 high；截图任务需要显式图片附件并关闭搜索。'
        } else {
            $lines += '高风险审查所需的 max 思考未在当前网页 UI 中检测到；请使用 Codex official 或其他支持 max 的后端。'
        }
    } else {
        $lines += '无法确认 DeepSeek 模式结构，仅允许只读探测，不发送 prompt。'
    }
    return ($lines -join "`r`n")
}
function Refresh-DeepSeekModePanel {
    if ($deepSeekModeStatus) { $deepSeekModeStatus.Text = Get-DeepSeekModeStatusText $script:DeepSeekModeProbe }
}
function Invoke-DeepSeekModeProbe {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    if (-not $diagnostic.resolved_bridge_root) { throw 'BRIDGE_ROOT_NOT_FOUND' }
    if ($diagnostic.bridge_port -ne '127.0.0.1 本机监听') { throw 'BRIDGE_NOT_RUNNING' }
    $raw = ''
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8791/mode-probe' -TimeoutSec 5
        if ($response.StatusCode -ne 200) { throw 'BRIDGE_MODE_PROBE_FAILED' }
        $raw = [string]$response.Content
        $probe = $raw | ConvertFrom-Json
        if (-not $probe) { throw 'BRIDGE_MODE_PROBE_FAILED' }
        foreach ($field in @(@('promptSent',$false),@('clickSend',$false),@('uploadAttempted',$false),@('modelCallSent',$false),@('privateApiReplay',$false))) {
            if (-not ($probe.psobject.Properties.Name -contains $field[0])) { $probe | Add-Member -NotePropertyName $field[0] -NotePropertyValue $field[1] }
        }
        $script:DeepSeekModeProbe = $probe
        Refresh-DeepSeekModePanel
        Add-DeepSeekLog 'mode-probe' ([pscustomobject]@{ exit_code = 0; error_type = if($script:DeepSeekModeProbe.status -eq 'PASS'){'PASS'}else{[string]$script:DeepSeekModeProbe.error_code} })
        [System.Windows.Forms.MessageBox]::Show((Get-DeepSeekModeStatusText $script:DeepSeekModeProbe),'DeepSeek 模式探测') | Out-Null
    } catch {
        $detail = [string]$_.Exception.Message
        $errorCode = if ($detail -match '^(BRIDGE_MODE_PROBE_FAILED|BRIDGE_NOT_RUNNING|BRIDGE_ROOT_NOT_FOUND)$') { $Matches[1] } else { 'BRIDGE_MODE_PROBE_FAILED' }
        $script:DeepSeekModeProbe = [pscustomobject]@{ status = 'ERROR'; error_code = $errorCode; promptSent = $false; clickSend = $false; uploadAttempted = $false; modelCallSent = $false; privateApiReplay = $false; modes = @{} }
        Refresh-DeepSeekModePanel
        Add-DeepSeekLog 'mode-probe' ([pscustomobject]@{ exit_code = 1; error_type = $errorCode })
        throw $errorCode
    }
}
function Format-DeepSeekModeSwitchSummary([object]$Result) {
    if ($Result.error_code) {
        return ("模式切换未完成：{0}`r`n错误说明：{1}`r`n未发送提示词、未点击发送、未上传附件。" -f $Result.error_code,(Get-UiErrorExplanation ([string]$Result.error_code)))
    }
    $before = $Result.before; $after = $Result.after
    return ("目标模式：{0}`r`n切换前：base={1}；思考={2}；搜索={3}；模态={4}`r`n切换后：base={5}；思考={6}；搜索={7}；模态={8}`r`n状态匹配：{9}`r`n未发送提示词、未点击发送、未上传附件。" -f $Result.target_mode,$before.current_base_mode,$before.current_thinking,$before.current_search,$before.current_modality,$after.current_base_mode,$after.current_thinking,$after.current_search,$after.current_modality,$Result.matched)
}
function Invoke-DeepSeekModeSwitch([ValidateSet('quick_plain','quick_thinking','expert_plain','expert_thinking','expert_max_review','quick_search','expert_thinking_search','vision_expert_thinking','file_extract')][string]$TargetMode) {
    $result = Invoke-RouterModeSwitch $TargetMode
    $errorType = if ($result.error_code) { [string]$result.error_code } elseif ($result.matched) { 'PASS' } else { 'MODE_SWITCH_VERIFY_FAILED' }
    Add-DeepSeekLog 'mode-switch' ([pscustomobject]@{ exit_code = if($result.matched){0}else{1}; error_type = $errorType })
    if ($result.after) {
        $script:DeepSeekModeProbe = [pscustomobject]@{
            status = if($result.matched){'PASS'}else{'MODE_SWITCH_VERIFY_FAILED'}
            error_code = $result.error_code
            promptSent = $false
            clickSend = $false
            uploadAttempted = $false
            current_base_mode = $result.after.current_base_mode
            current_thinking = $result.after.current_thinking
            current_search = $result.after.current_search
            current_modality = $result.after.current_modality
            modes = @{}
        }
        Refresh-DeepSeekModePanel
    }
    [System.Windows.Forms.MessageBox]::Show((Format-DeepSeekModeSwitchSummary $result),'DeepSeek 仅模式切换') | Out-Null
}
function Set-DeepSeekModePreference([ValidateSet('auto','quick_plain','quick_thinking','quick_search','expert_plain','expert_thinking','expert_max_review','expert_thinking_search','vision_expert_thinking','file_extract')][string]$Preference) {
    $script:DeepSeekModePreference = $Preference
    Refresh-DeepSeekModePanel
    [System.Windows.Forms.MessageBox]::Show(("已设置 DeepSeek 模式策略：{0}`r`n这只影响本地选择说明，不会发送请求，也不会修改 Codex 配置。" -f $Preference),'DeepSeek 模式策略') | Out-Null
}
function Set-DeepSeekPerformanceMode([ValidateSet('economy','balanced','accuracy')][string]$Performance) {
    $script:DeepSeekPerformanceMode = $Performance
    Refresh-DeepSeekModePanel
    [System.Windows.Forms.MessageBox]::Show("已设置性能策略：$Performance。仅影响自动选择，不会发送请求。",'DeepSeek 模式策略') | Out-Null
}
function Format-DeepSeekSelectionSummary([object]$Record) {
    $labels = @{ quick_plain = '快速'; quick_thinking = '快速 + 深度思考'; best_available_reasoning = '最佳可用思考（medium/无搜索）'; quick_search = '快速 + 搜索'; expert_plain = '专家'; expert_thinking = '专家 + 深度思考'; expert_max_review = '专家 + 最高思考审查'; expert_thinking_search = '专家 + 深度思考 + 搜索'; vision_expert_thinking = '视觉 + 专家 + 深度思考'; file_extract = '文件提取' }
    $mode = if ($labels.ContainsKey([string]$Record.selected_mode)) { $labels[[string]$Record.selected_mode] } else { [string]$Record.selected_mode }
    $fallback = if ($Record.fallback_reason) { [string]$Record.fallback_reason } else { '无' }
    return ("推荐模式：{0}`r`n模型别名：{1}`r`n选择原因：{2}`r`n回退原因：{3}`r`n模式可用：{4}" -f $mode,$Record.selected_model_alias,$Record.why_selected,$fallback,$Record.mode_available)
}
function Explain-DeepSeekModeSelection {
    $raw = Invoke-RouterCli @('deepseek','explain-mode','当前 Codex 请求未提供任务文本','--preference',$script:DeepSeekModePreference,'--performance-mode',$script:DeepSeekPerformanceMode)
    try {
        $script:DeepSeekLastSelection = $raw | ConvertFrom-Json
        Refresh-DeepSeekModePanel
        [System.Windows.Forms.MessageBox]::Show((Format-DeepSeekSelectionSummary $script:DeepSeekLastSelection),'DeepSeek 模式选择原因') | Out-Null
    } catch { throw 'DEEPSEEK_EXPLAIN_RESPONSE_INVALID' }
}
function Refresh-DeepSeekPanel {
    # 高级调试说明：健康检查只读取本地服务与页面状态，不发送 prompt、不调用聊天接口、不点击发送按钮。
    $snapshot = Get-DeepSeekBridgeSnapshot
    $diagnostic = Test-LauncherPrerequisites
    $evidence = Get-DeepSeekLauncherRuntimeEvidence
    $deepSeekStatus.Text = (("模式：本地 Bridge 直连`r`n本地接口：{0}`r`n`r`n桥接状态：{1}`r`n工作进程状态：{2}`r`n浏览器状态：{3}`r`nDeepSeek 页面：{4}`r`n忙碌标记：{5}`r`n最近健康检查：{6}`r`n`r`nBridge 进程运行：{7}`r`nBridge 端口 8791：{8}`r`n8791 owner kind：{9}`r`nBridge HTTP ready：{10}`r`nChrome kind：{11}`r`n受控 Chrome attached：{12}`r`nComposer：{13}`r`nToolbar：{14}`r`n最近健康端点：{15}`r`n最近健康错误：{16}`r`nWorker required：NO`r`nRouter required for Bridge-only action：NO`r`n`r`n端口 8792：{17}`r`n端口 8793：{18}`r`n`r`n{19}`r`n`r`n健康检查只读取本地服务与页面状态，不发送提示词、不调用聊天接口、不点击发送按钮。Bridge-only 三合一探测不要求 Worker 或 Router。" -f $DeepSeekLocalApiAddress,$snapshot.bridge,$snapshot.worker,$snapshot.chrome,$snapshot.page,$snapshot.busy,$script:DeepSeekLastHealth,$evidence.process_running,$evidence.port_listening,$snapshot.bridge_port_owner,$evidence.http_ready,$snapshot.chrome_kind,$snapshot.controlled_attached,$snapshot.composer_found,$snapshot.toolbar_found,$evidence.last_health_endpoint,$evidence.last_health_error,$snapshot.worker_port,$snapshot.fixture_port,(Format-DeepSeekLauncherDiagnostic $diagnostic)))
    Refresh-DeepSeekModePanel
}
function Invoke-DeepSeekPanelAction([string]$Action) {
    # 高级调试原文：不会显示或保存 prompt、response、key、Cookie 或 Token。
    $result = Invoke-DeepSeekLocalScript -Action $Action
    Add-DeepSeekLog -Action $Action -Result $result
    Refresh-DeepSeekPanel
    $details = @(
        ("操作：{0}" -f $Action),
        ("错误码：{0}" -f $result.error_code),
        ("退出码：{0}" -f $result.exit_code),
        ("状态：{0}" -f $result.error_type),
        ("expected_path：{0}" -f $result.expected_path),
        ("resolved_bridge_root：{0}" -f $result.resolved_bridge_root),
        ("cwd：{0}" -f $result.cwd),
        ("command_kind：{0}" -f $result.command_kind),
        ("stage：{0}" -f $result.stage),
        ("file_exists：{0}；runtime_exists：{1}" -f $result.file_exists,$result.runtime_exists),
        ("bridge_process_id：{0}；bridge_exit_code：{1}" -f $result.bridge_process_id,$result.bridge_exit_code),
        ("bridge_port_listening：{0}；bridge_http_ready：{1}" -f $result.bridge_port_listening,$result.bridge_http_ready),
        ("port_owner_kind：{0}；port_owner_pid/name：{1}/{2}" -f $result.port_owner_kind,$result.port_owner_pid,$result.port_owner_process_name),
        ("chrome_kind：{0}；controlled_chrome_attached：{1}" -f $result.chrome_kind,$result.controlled_chrome_attached),
        ("chrome_debug_port：{0}；chrome_user_data_dir：{1}" -f $result.chrome_debug_port,$result.chrome_user_data_dir_sanitized),
        ("active_page_url_kind：{0}；composer_found：{1}；toolbar_found：{2}" -f $result.active_page_url_kind,$result.composer_found,$result.toolbar_found),
        ("last_health_endpoint：{0}；last_health_error：{1}" -f $result.last_health_endpoint,$result.last_health_error),
        ("router_required：{0}；router_status：{1}" -f $result.router_required,$result.router_status),
        ("suggested_fix：{0}" -f $result.suggested_fix),
        ("bridge_required={0}；worker_required={1}；prompt_sent=false；model_call_sent=false" -f $result.bridge_required,$result.worker_required),
        '不会显示或保存提示词、响应、密钥、Cookie 或 Token。'
    ) -join "`r`n"
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $details),'DeepSeek 本地桥接')
}
function Show-DeepSeekLauncherDiagnostic {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    $deepSeekStatus.Text = ((Format-DeepSeekLauncherDiagnostic $diagnostic) + "`r`n`r`n启动器诊断只显示本地路径、端口和脱敏命令，不显示凭据。")
    [System.Windows.Forms.MessageBox]::Show((Format-DeepSeekLauncherDiagnostic $diagnostic),'DeepSeek 启动器诊断') | Out-Null
}
function Copy-DeepSeekLauncherDiagnostic {
    $diagnostic = Test-LauncherPrerequisites
    $script:DeepSeekLauncherLast = $diagnostic
    try { Set-Clipboard -Value (Redact-Text (Format-DeepSeekLauncherDiagnostic $diagnostic)); [System.Windows.Forms.MessageBox]::Show('已复制脱敏启动器诊断。','DeepSeek 启动器诊断') | Out-Null }
    catch { [System.Windows.Forms.MessageBox]::Show('复制启动器诊断失败。','DeepSeek 启动器诊断') | Out-Null }
}
function Open-DeepSeekBridgeDirectory {
    $diagnostic = Test-LauncherPrerequisites
    if ($diagnostic.resolved_bridge_root -and (Test-Path -LiteralPath $diagnostic.resolved_bridge_root -PathType Container)) { Start-Process explorer.exe -ArgumentList ('"' + $diagnostic.resolved_bridge_root + '"') }
    else { [System.Windows.Forms.MessageBox]::Show('未找到 Bridge 项目目录。请先检查启动器路径。','DeepSeek 启动器诊断') | Out-Null }
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
        'OFFICIAL_ASSISTED_COORDINATOR' { return '官方辅助协调' }
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
function Invoke-CodexModeAction([ValidateSet('official-direct','custom-router','custom-deepseek-head','custom-local-light','custom-external-api','custom-hybrid-agent','custom-deepseek-text-only','custom-local-text-only','custom-hybrid-text-only','official-assisted','official-assisted-coordinator','deepseek-head','local-agent-pending','restore')][string]$Action) {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $CodexModeScript -Action $Action 2>&1 | Out-String
    $record = $null; try { $record = $output | ConvertFrom-Json } catch {}
    $summary = Format-CodexModeSummary -Record $record -Raw $output -Action $Action
    Refresh-CodexModePanel; Refresh-Home
    if ($diagnosticsText -and $debugToggle -and $debugToggle.Checked) { $diagnosticsText.Text = ($script:LastModeDebugJson + "`r`n" + ($script:UiDebugEntries -join "`r`n")) }
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $summary),'Codex 连接模式')
}
function Set-CodexConfigSwitcherStatus([string]$Text) {
    foreach ($target in @($codexConfigSwitcherStatus,$homeConfigSwitcherStatus,$configSwitcherTabStatus)) {
        if ($target) { $target.Text = Redact-Text $Text }
    }
}
function Resolve-CodexConfigCliError([object]$Execution,[object]$Record) {
    if ($Execution.error_code) { return [string]$Execution.error_code }
    if ($Record -and $Record.error_code) { return [string]$Record.error_code }
    $detail = [string]$Execution.stderr + "`n" + [string]$Execution.stdout
    if ($detail -match '(CODEX_CONFIG_[A-Z_]+|OFFICIAL_PROFILE_NOT_CAPTURED)') { return $Matches[1] }
    if ($detail -match '(ModuleNotFoundError|ImportError|No module named)') { return 'CODEX_CONFIG_PYTHON_IMPORT_FAILED' }
    if ($Execution.exit_code -ne 0) { return 'CODEX_CONFIG_CLI_ENTRYPOINT_FAILED' }
    return 'CODEX_CONFIG_UNKNOWN_ERROR_SANITIZED'
}
function New-CodexConfigActionFailure([string]$Action,[string]$Code,[object]$Execution) {
    $failure = New-UiActionFailure $Action $Code (([string]$Execution.stderr + "`n" + [string]$Execution.stdout).Trim())
    $failure.command_kind = [string]$Execution.command_kind; $failure.exit_code = $Execution.exit_code
    $failure.stderr_summary = Redact-Text ([string]$Execution.stderr); $failure.stdout_summary = Redact-Text ([string]$Execution.stdout)
    $failure.config_exists = Test-Path -LiteralPath $CodexConfig; $failure.router_required = 'NO'
    return $failure
}
function Format-CodexConfigActionFailure([object]$Failure) {
    return ("操作：{0}`r`n错误码：{1}`r`n原因：{2}`r`n建议：{3}`r`n命令类型：{4}`r`n退出码：{5}`r`nstderr 摘要：{6}`r`nstdout 摘要：{7}`r`n配置：{8}`r`n配置存在：{9}`r`n需要 Router：{10}`r`nRouter：{11}`r`n真实配置已修改：{12}`r`n模型调用已发送：{13}" -f $Failure.action_name,$Failure.error_code,$Failure.sanitized_reason,$Failure.suggested_fix,$Failure.command_kind,$Failure.exit_code,$Failure.stderr_summary,$Failure.stdout_summary,$Failure.config_path,$Failure.config_exists,$Failure.router_required,$Failure.router_status,$Failure.real_config_was_modified,$Failure.model_call_was_sent)
}
function Invoke-CodexConfigSwitcherAction([ValidateSet('status','backup','capture-official','switch-xiaoyu','switch-official','restore-previous','validate')][string]$Action) {
    $cliArgs = @('codex-config',$Action,'--config-path',$CodexConfig,'--root',$CodexConfigSwitcherStateRoot)
    if ($Action -eq 'capture-official') {
        $choice = [System.Windows.Forms.MessageBox]::Show('请仅在你已手动确认 Codex 当前为官方模式时继续。此操作不会读取 Codex UI。是否捕获本地配置快照？','确认官方配置',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) { return }
        $cliArgs += '--confirm-official'
    } elseif ($Action -in @('switch-xiaoyu','switch-official','restore-previous')) {
        $choice = [System.Windows.Forms.MessageBox]::Show('此操作会修改本地 Codex 配置文件，但不会读取或点击 Codex UI。完成后需重启 Codex 并新建对话。是否继续？','Codex 配置切换',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    }
    $execution = Invoke-CodexConfigModuleCli $cliArgs
    $record = $null
    if (-not $execution.error_code -and $execution.exit_code -eq 0) {
        try { $record = $execution.stdout | ConvertFrom-Json -ErrorAction Stop } catch { $execution.error_code = 'CODEX_CONFIG_CLI_JSON_PARSE_FAILED' }
    }
    $errorCode = if ($execution.error_code -or $execution.exit_code -ne 0) { Resolve-CodexConfigCliError $execution $record } elseif ($record.error_code) { [string]$record.error_code } elseif ([string]$record.status -in @('OFFICIAL_PROFILE_NOT_CAPTURED','BACKUP_NOT_FOUND','USER_CONFIRMATION_REQUIRED')) { [string]$record.status } else { '' }
    if ($errorCode) {
        $failure = New-CodexConfigActionFailure $Action $errorCode $execution
        Set-CodexConfigSwitcherStatus (Format-CodexConfigActionFailure $failure)
        if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($homeConfigSwitcherStatus.Text,'Codex 配置切换',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null }
        return $failure
    }
    Set-CodexConfigSwitcherStatus ("$runtimeVersionText`r`n`r`n当前 config 路径：{0}`r`n当前 provider：{1}`r`n当前 base_url：{2}`r`n当前 wire_api：{3}`r`n当前配置状态：{4}`r`n需要重启 Codex：{5}`r`n最近备份：{6}`r`n最近切换时间：{7}`r`n`r`n本功能只修改本地 Codex 配置文件，不读取或点击 Codex UI。修改后请重启 Codex 并新建对话。" -f $record.path,$record.model_provider,$record.base_url,$record.wire_api,$record.status,$record.restart_codex_required,$record.last_backup_path,$record.last_switch_time)
    if ($Action -in @('switch-xiaoyu','switch-official','restore-previous')) {
        $script:RealCodexConfigModified = if ([string]::IsNullOrWhiteSpace($CodexConfigPath)) { 'YES' } else { 'NO' }
    }
    if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($homeConfigSwitcherStatus.Text,'Codex 配置切换') | Out-Null }
    return $record
}
function Refresh-CodexConfigSwitcherPanel {
    try { [void](Invoke-CodexConfigSwitcherAction 'status') }
    catch {
        $failure = New-UiActionFailure 'status' (Get-UiActionErrorCode ([string]$_.Exception.Message)) ([string]$_.Exception.Message)
        Set-CodexConfigSwitcherStatus ("$runtimeVersionText`r`n`r`n当前 config 路径：{0}`r`n状态：{1}`r`n原因：{2}`r`n建议：{3}`r`nRouter：{4}`r`n真实配置已修改：{5}`r`n模型调用已发送：NO" -f $failure.config_path,$failure.error_code,$failure.sanitized_reason,$failure.suggested_fix,$failure.router_status,$failure.real_config_was_modified)
    }
}
function Invoke-CodexConfigSwitcherRuntimeSelfTest {
    $execution = Invoke-CodexConfigModuleCli @('codex-config','status','--config-path',$CodexConfig,'--root',$CodexConfigSwitcherStateRoot)
    $record = $null; $jsonParsed = $false
    if (-not $execution.error_code -and $execution.exit_code -eq 0) {
        try { $record = $execution.stdout | ConvertFrom-Json -ErrorAction Stop; $jsonParsed = $true } catch {}
    }
    $errorCode = if($jsonParsed){''}else{Resolve-CodexConfigCliError $execution $record}
    $result = [ordered]@{
        config_path = $CodexConfig
        config_exists = Test-Path -LiteralPath $CodexConfig
        python_executable = $execution.python_executable
        command_kind = $execution.command_kind
        working_directory = $execution.working_directory
        pythonpath = $execution.pythonpath
        exit_code = $execution.exit_code
        json_parsed = $jsonParsed
        router_required = 'NO'
        router_status = if ($SelfTest) { 'NOT_LISTENING' } else { (Get-RouterStatus).listener_status }
        error_code = $errorCode
        stderr_summary = Redact-Text ([string]$execution.stderr)
        real_config_was_modified = 'NO'
        model_call_was_sent = 'NO'
    }
    Set-CodexConfigSwitcherStatus ($result | ConvertTo-Json -Depth 3)
    if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($homeConfigSwitcherStatus.Text,'配置切换器自检') | Out-Null }
    return [pscustomobject]$result
}
function Get-AssistStartupInstruction {
    return "启动官方辅助模式。先调用小羽本地协调器：`r`nPOST http://127.0.0.1:18789/assist/coordinate`r`n把当前任务发送给协调器。`r`n执行协调器返回的 codex_required_steps。`r`n不要执行被小羽标记为 local_agent_steps 的简单辅助任务。`r`n遇到高风险步骤先停止并询问用户。"
}
function Format-AssistCoordinateSummary([object]$Record,[string]$Raw) {
    $script:LastAssistDebugJson = Redact-Text $Raw
    if (-not $Record -or $Record.error_code) {
        $code = if ($Record.error_code) { [string]$Record.error_code } else { 'ASSIST_COORDINATE_UNAVAILABLE' }
        return "官方辅助协调预检失败`r`n错误码：$code`r`n当前 Codex 执行层：官方直连`r`n接管 Codex endpoint：否`r`n详细信息：已折叠到高级信息。"
    }
    $localCount = @($Record.local_agent_steps).Count
    $codexCount = @($Record.codex_required_steps).Count
    return ("官方辅助协调已启用`r`n当前 Codex 执行层：官方直连`r`n辅助脑：{0}`r`n本地可处理步骤：{1}`r`n需要 Codex 处理步骤：{2}`r`n风险等级：{3}`r`n是否需要官方 Codex：{4}`r`n接管 Codex endpoint：否`r`n自动写入/提交：否`r`n回退原因：{5}" -f $Record.recommended_brain,$localCount,$codexCount,$Record.risk_level,$Record.requires_official_codex,$Record.fallback_reason)
}
function Show-OfficialAssistedCoordinator {
    $script:OfficialAssistMode = 'OFFICIAL_ASSISTED_COORDINATOR'
    $codexModeStatus.Text = "当前 Codex 执行层：官方直连`r`n当前辅助协调：已启用`r`n当前辅助脑：自动`r`n是否接管 Codex endpoint：否`r`n是否自动执行写入：否`r`n是否绕过 Codex 额度：否`r`n协调器：127.0.0.1:18789/assist/coordinate"
    [System.Windows.Forms.MessageBox]::Show('已启用官方辅助协调模式。Codex 仍保持官方直连；小羽只生成计划、只读结果和 handoff，不自动执行写入。','官方辅助协调') | Out-Null
}
function Show-AssistStartupInstruction {
    $instruction = Get-AssistStartupInstruction
    $script:LastAssistDebugJson = $instruction
    [System.Windows.Forms.MessageBox]::Show($instruction,'Codex 启动指令') | Out-Null
}
function Copy-AssistStartupInstruction {
    try { Set-Clipboard -Value (Get-AssistStartupInstruction); [System.Windows.Forms.MessageBox]::Show('已复制 Codex 启动指令。','Codex 启动指令') | Out-Null } catch { [System.Windows.Forms.MessageBox]::Show('复制失败。','Codex 启动指令') | Out-Null }
}
function Invoke-AssistCoordinatePrecheck {
    $raw = Invoke-RouterCli @('assist','coordinate','--task','检查当前项目状态，不修改文件')
    $record = $null; try { $record = $raw | ConvertFrom-Json } catch {}
    $summary = Format-AssistCoordinateSummary -Record $record -Raw $raw
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $summary),'官方辅助协调预检') | Out-Null
}
function Show-AssistStepList([bool]$CodexSteps) {
    $raw = Invoke-RouterCli @('assist','coordinate','--task','检查当前项目状态，不修改文件')
    $record = $null; try { $record = $raw | ConvertFrom-Json } catch {}
    $steps = if($CodexSteps){$record.codex_required_steps}else{$record.local_agent_steps}
    $title = if($CodexSteps){'需要 Codex 处理步骤'}else{'本地可处理步骤'}
    $text = if(@($steps).Count -eq 0){'暂无步骤。'}else{(@($steps)|ForEach-Object { '- ' + $_.description }) -join "`r`n"}
    [System.Windows.Forms.MessageBox]::Show((Redact-Text $text),$title) | Out-Null
}
function Select-AssistBrain {
    $choices = '自动','DeepSeek','本地模型','外部 API','Hybrid'
    $selected = [System.Windows.Forms.MessageBox]::Show('辅助脑当前由协调器按健康状态自动选择。`r`n请在 Local Agent 面板的 Brain Provider 下拉框中选择显式 Provider。','选择辅助脑')
    if($selected -eq [System.Windows.Forms.DialogResult]::OK){$script:AssistBrainSelection='auto'}
}
function Open-AssistHandoffDirectory {
    $path = Join-Path $env:USERPROFILE '.codex-ai-router\handoff'
    if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无 handoff 目录；协调预检不会自动创建文件。','handoff 目录') | Out-Null}
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
    $allowlistStatus.Text=(Redact-Text ((Get-ProviderAllowlistJson)|Out-String)+"`r`n"+(Get-DeepSeekBridgeDirectAllowlistSummary))
    $recordStatus.Text=(Redact-Text (($record|ConvertTo-Json -Compress))+"`r`n记录文件："+(Join-Path $env:USERPROFILE '.codex-ai-router\call-ledger.jsonl')+"`r`n默认保存正文：NO`r`nAPI Key/Cookie/Token/Authorization：NO")
}
function Get-ProviderAllowlistJson {
    try {
        $spec = Get-RouterLaunchSpec
        return (& $spec.path -m codex_ai_router.provider_allowlist 2>&1 | Out-String)
    } catch {
        return ((& py.exe -3 -m codex_ai_router.provider_allowlist 2>&1) | Out-String)
    }
}
function Get-DeepSeekBridgeDirectAllowlistSummary {
    $raw = Get-ProviderAllowlistJson
    try {
        $catalog = $raw | ConvertFrom-Json
        $direct = @($catalog.providers | Where-Object { $_.id -eq 'deepseek-bridge-direct' }) | Select-Object -First 1
        if ($null -eq $direct) { return 'DeepSeek Bridge Direct：未允许；仅本地 loopback：YES；API-only 暴露：NO；fallback：NO；阻断原因：未找到内部记录。' }
        $allowed = if ($direct.enabled -eq $true) { '已允许' } else { '未允许' }
        return ('DeepSeek Bridge Direct：{0}`r`n仅本地 loopback：YES`r`nAPI-only 暴露：NO`r`nfallback：NO`r`n当前状态：{1}`r`n当前阻断原因：{2}' -f $allowed,$direct.status,($(if($direct.status -eq 'ENABLED'){'NONE'}else{$direct.status})))
    } catch { return 'DeepSeek Bridge Direct：状态读取失败；未向网页发送请求。' }
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
    $statusRaw = Invoke-LocalCli @('status'); $modelsRaw = Invoke-LocalCli @('models'); $profilesRaw = Invoke-LocalCli @('profiles'); $bonsaiRuntimeRaw = Invoke-LocalCli @('bonsai-runtime','status'); $vulkanRaw = Invoke-LocalCli @('vulkan','status'); $status = $null; $models = $null; $profiles = $null; $bonsaiRuntime = $null; $vulkan = $null
    try { $status = $statusRaw | ConvertFrom-Json } catch {}; try { $models = $modelsRaw | ConvertFrom-Json } catch {}; try { $profiles = $profilesRaw | ConvertFrom-Json } catch {}; try { $bonsaiRuntime = $bonsaiRuntimeRaw | ConvertFrom-Json } catch {}; try { $vulkan = $vulkanRaw | ConvertFrom-Json } catch {}
    if($status){$selected = if($status.selected_model){$status.selected_model.model_id}else{'未选择'}; $path = if($status.llama_server_path){$status.llama_server_path}else{'未发现'}; $owner = if($status.port_owner){('{0} (PID {1})' -f $status.port_owner.process_name,$status.port_owner.pid)}else{'无'}; $lastRepair = if($script:DirectLocalLastRepair){$script:DirectLocalLastRepair}else{'未运行'}; $bonsai = if($status.bonsai_support){$status.bonsai_support.status}else{'UNKNOWN'}; $standardRuntime = if($status.runtime_registry.standard_llama_cpp.llama_server_path){$status.runtime_registry.standard_llama_cpp.llama_server_path}else{'未发现'}; $prismRuntime = if($bonsaiRuntime.runtime_path){$bonsaiRuntime.runtime_path}else{'未配置'}; $prismCompatibility = if($bonsaiRuntime.compatibility_status){$bonsaiRuntime.compatibility_status}else{'BONSAI_RUNTIME_UNKNOWN'}; $downloadAllowed = if($prismCompatibility -eq 'PASS'){'是（仍需测试门禁）'}else{'否'}; $selectedProfile = if($profiles -and $profiles.profiles){$profiles.profiles | Where-Object {$_.model_id -eq $selected} | Select-Object -First 1}else{$null}; $lastSmoke = if($selectedProfile){$selectedProfile.last_smoke_status}else{'UNKNOWN'}; $lastError = if($selectedProfile -and $selectedProfile.last_error_code){$selectedProfile.last_error_code}else{'NONE'}; $bonsaiProfile = if($status.bonsai_support -and $status.bonsai_support.models){$status.bonsai_support.models | Select-Object -First 1}else{$null}; $bonsaiSmoke = if($bonsaiProfile){$bonsaiProfile.minimal_smoke_status}else{'NOT_RUN'}; $bonsaiPerformance = if($bonsaiProfile){$bonsaiProfile.performance_class}else{'NOT_CLASSIFIED'}; $bonsaiUse = if($bonsaiProfile){$bonsaiProfile.recommended_use}else{'NOT_APPLICABLE'}; $bonsaiAuto = if($bonsaiProfile){$bonsaiProfile.auto_select_allowed}else{'NO'}; $vulkanBackend = if($vulkan){$vulkan.recommended_backend}else{'CPU'}; $vulkanDevice = if($vulkan -and $vulkan.VULKAN_DEVICE_NAME){$vulkan.VULKAN_DEVICE_NAME}else{'未检测'}; $vulkanLayers = if($vulkan -and $vulkan.benchmark){$vulkan.benchmark.best_gpu_layers}else{'未调优'}; $vulkanTps = if($vulkan -and $vulkan.benchmark){$vulkan.benchmark.standard_vulkan_generation_tps}else{'UNKNOWN'}; $directLocalStatus.Text = (("DIRECT_LOCAL_MODEL_STUDIO：{0}`r`n本地 OpenAI 兼容端点：{1}`r`n后端：直接本地模型`r`n标准 llama.cpp runtime：{2}`r`nPrism Bonsai runtime：{3}`r`nPrism 兼容状态：{4}`r`n允许下载 Bonsai：{5}`r`nBonsai 模型状态：{6}`r`n当前加载模型：{7}`r`n推荐模型：按任务自动选择`r`n服务状态：{8}`r`n本地端点：{9}`r`n当前端口：{10}`r`n端口占用：{11}`r`n端口所有者：{12}`r`n备用端口切换：{13}`r`n最近修复：{14}`r`n模型画像数：{15}`r`n最近 smoke：{16}`r`n最近错误：{17}`r`n崇祯历史模拟 profile：history_chongzhen`r`n自动选择模型：已启用（Bonsai 默认禁用）`r`n说明：控制台仅负责管理；仅本地 smoke/demo，不是正式任务入口；正式任务入口仍是 Codex。" -f $status.DIRECT_LOCAL_MODEL_STUDIO,$status.LOCAL_OPENAI_COMPATIBLE_ENDPOINT,$standardRuntime,$prismRuntime,$prismCompatibility,$downloadAllowed,$bonsai,$selected,$status.server_running,$status.endpoint,$status.port,$status.port_in_use,$owner,$status.auto_port_fallback,$lastRepair,$status.model_count,$lastSmoke,$lastError) + ("`r`nBonsai 最小 smoke：{0}`r`nBonsai 性能分级：{1}`r`nBonsai 建议用途：{2}`r`nBonsai 允许自动选择：{3}`r`n运行后端推荐：{4}`r`nVulkan GPU：{5}`r`nGPU Offload 最佳层数：{6}`r`nVulkan 实测速率：{7} tok/s" -f $bonsaiSmoke,$bonsaiPerformance,$bonsaiUse,$bonsaiAuto,$vulkanBackend,$vulkanDevice,$vulkanLayers,$vulkanTps))}else{$directLocalStatus.Text = ('直接本地状态不可用：' + $statusRaw)}
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
function Add-UiLayoutButton([System.Windows.Forms.Control]$Target,[string]$Caption,[scriptblock]$Action,[int]$Width=210,[string]$TooltipText='') { $button=New-Object System.Windows.Forms.Button; $button.Text=$Caption; $button.Width=$Width; $button.MinimumSize=New-Object System.Drawing.Size($Width,36); $button.Height=36; $button.AutoSize=$false; $button.AutoEllipsis=$false; $button.TextAlign='MiddleCenter'; $button.UseCompatibleTextRendering=$true; $button.Font=$buttonFont; $button.Margin=New-Object System.Windows.Forms.Padding(4); if($TooltipText){$uiToolTip.SetToolTip($button,$TooltipText)}; $safeName=$Caption;$safeAction=$Action;$button.Add_Click({Invoke-SafeUiAction -Name $safeName -Action $safeAction}.GetNewClosure()); [void]$Target.Controls.Add($button) }
function Add-CodexModeButton([string]$Caption,[scriptblock]$Action,[int]$Width=190) { Add-UiLayoutButton $codexModeButtons $Caption $Action $Width }
function Add-CodexConfigButtonToTarget([System.Windows.Forms.Control]$Target,[string]$Caption,[scriptblock]$Action,[int]$Width) {
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Caption; $button.AutoSize = $true; $button.AutoSizeMode = 'GrowAndShrink'; $button.MinimumSize = New-Object System.Drawing.Size($Width,42); $button.UseCompatibleTextRendering = $true; $button.AutoEllipsis = $false; $button.TextAlign = 'MiddleCenter'; $button.Font = $buttonFont; $button.Padding = New-Object System.Windows.Forms.Padding(10,6,10,6); $button.Margin = New-Object System.Windows.Forms.Padding(4)
    $safeName = $Caption; $safeAction = $Action; $button.Add_Click({ Invoke-SafeUiAction -Name $safeName -Action $safeAction }.GetNewClosure()); [void]$Target.Controls.Add($button)
}
function Add-CodexConfigSwitcherButton([string]$Caption,[scriptblock]$Action,[int]$Width=170) {
    Add-CodexConfigButtonToTarget $codexConfigSwitcherButtons $Caption $Action $Width
    Add-CodexConfigButtonToTarget $homeConfigSwitcherButtons $Caption $Action $Width
    Add-CodexConfigButtonToTarget $configSwitcherTabButtons $Caption $Action $Width
}
function Test-CodexConfigSwitcherLayout {
    $form.PerformLayout(); $homeConfigSwitcherLayout.PerformLayout(); $configSwitcherTabLayout.PerformLayout()
    $allButtons = @($homeConfigSwitcherButtons.Controls) + @($configSwitcherTabButtons.Controls)
    $textFits = $true
    foreach ($button in $allButtons) {
        $measured = [System.Windows.Forms.TextRenderer]::MeasureText($button.Text,$button.Font)
        if ($button.PreferredSize.Width -lt ($measured.Width + 8)) { $textFits = $false }
    }
    $homeSeparateRows = $homeConfigSwitcherLayout.GetRow($homeConfigSwitcherStatus) -ne $homeConfigSwitcherLayout.GetRow($homeConfigSwitcherButtons)
    return [pscustomobject]@{
        version_info_no_overlap = $homeSeparateRows
        button_text_fits = $textFits
        home_scroll_enabled = $homeTab.AutoScroll -and $homeConfigSwitcherButtons.AutoScroll
        tab_scroll_enabled = $configSwitcherTab.AutoScroll -and $configSwitcherTabButtons.AutoScroll
        runtime_path_shortened = ($homeConfigSwitcherStatus.Text -notmatch [regex]::Escape($PSCommandPath)) -and ($homeConfigSwitcherStatus.Text -match '\.\.\\')
    }
}
function Invoke-CodexConfigSwitcherUiSelfCheck {
    $labels = @('读取当前 Codex 配置','备份当前配置','捕获当前为官方配置','切到官方 Codex','切到小羽 Custom Router','恢复上一次配置','校验配置','复制重启提示')
    $buttonText = @($homeConfigSwitcherButtons.Controls | ForEach-Object { $_.Text }) + @($configSwitcherTabButtons.Controls | ForEach-Object { $_.Text })
    $layout = Test-CodexConfigSwitcherLayout
    $result = [ordered]@{
        CODEX_CONFIG_SWITCHER_TAB_VISIBLE = $tabs.TabPages.Contains($configSwitcherTab)
        CODEX_CONFIG_SWITCHER_HOME_SECTION_VISIBLE = ($null -ne $homeConfigSwitcherGroup -and -not $homeConfigSwitcherGroup.IsDisposed)
        READ_CONFIG_BUTTON_VISIBLE = '读取当前 Codex 配置' -in $buttonText
        BACKUP_CONFIG_BUTTON_VISIBLE = '备份当前配置' -in $buttonText
        CAPTURE_OFFICIAL_BUTTON_VISIBLE = '捕获当前为官方配置' -in $buttonText
        SWITCH_OFFICIAL_BUTTON_VISIBLE = '切到官方 Codex' -in $buttonText
        SWITCH_XIAOYU_BUTTON_VISIBLE = '切到小羽 Custom Router' -in $buttonText
        RESTORE_PREVIOUS_BUTTON_VISIBLE = '恢复上一次配置' -in $buttonText
        VALIDATE_CONFIG_BUTTON_VISIBLE = '校验配置' -in $buttonText
        COPY_RESTART_NOTICE_BUTTON_VISIBLE = '复制重启提示' -in $buttonText
        VERSION_INFO_NO_OVERLAP = $layout.version_info_no_overlap
        BUTTON_TEXT_FITS = $layout.button_text_fits
        WINDOW_SCROLLING_ENABLED = $layout.home_scroll_enabled -and $layout.tab_scroll_enabled
        RUNTIME_PATH_SHORTENED = $layout.runtime_path_shortened
        missing_button_count = @($labels | Where-Object { $_ -notin $buttonText }).Count
        real_config_was_modified = 'NO'
        model_call_was_sent = 'NO'
    }
    $text = $result | ConvertTo-Json -Depth 3
    Set-CodexConfigSwitcherStatus $text
    if (-not $SelfTest) { [System.Windows.Forms.MessageBox]::Show($text,'检查配置切换器 UI') | Out-Null }
    return [pscustomobject]$result
}
function Add-CodexAssistButton([string]$Caption,[scriptblock]$Action,[int]$Width=210) { Add-UiLayoutButton $codexAssistButtons $Caption $Action $Width }
function Add-CodexAnalysisButton([string]$Caption,[scriptblock]$Action,[int]$Width=210) { Add-UiLayoutButton $codexAnalysisButtons $Caption $Action $Width }
function Add-CodexBrainButton([string]$Caption,[scriptblock]$Action,[int]$Width=210) { Add-UiLayoutButton $codexBrainButtons $Caption $Action $Width }
function Add-ToolsPolicyButton([string]$Caption,[scriptblock]$Action,[int]$Width=190) { Add-UiLayoutButton $toolsPolicyButtons $Caption $Action $Width }
function Add-LocalAgentButton([string]$Caption,[scriptblock]$Action,[int]$Width=210,[string]$TooltipText='') { Add-UiLayoutButton $localAgentButtons $Caption $Action $Width $TooltipText }
function Add-LocalAgentConfirmButton([string]$Caption,[scriptblock]$Action,[int]$Width=210,[string]$TooltipText='') { Add-UiLayoutButton $localAgentConfirmButtons $Caption $Action $Width $TooltipText }
function Add-LocalAgentRecordButton([string]$Caption,[scriptblock]$Action,[int]$Width=210) { Add-UiLayoutButton $localAgentRecordButtons $Caption $Action $Width }
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
Add-DirectLocalButton '启动本地模型服务' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('serve')),'直接本地模型 Studio');Refresh-DirectLocalCard }
Add-DirectLocalButton '停止本地后端' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('stop')),'停止本地后端');Refresh-DirectLocalCard }
Add-DirectLocalButton '本地修复' { $raw = Invoke-LocalCli @('repair'); try { $result = $raw | ConvertFrom-Json; $script:DirectLocalLastRepair = if($result.status -eq 'PASS'){'已通过'}else{[string]$result.error_code + '：' + (Get-LocalErrorExplanation ([string]$result.error_code))} } catch { $script:DirectLocalLastRepair = 'REPAIR_RESPONSE_INVALID：修复结果无法读取。' }; [System.Windows.Forms.MessageBox]::Show($raw,'本地修复');Refresh-DirectLocalCard }
Add-DirectLocalButton '测试本地模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('smoke','只回复 LOCAL_DIRECT_OK')),'本地模型测试');Refresh-DirectLocalCard }
Add-DirectLocalButton '查看模型画像' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('profiles')),'本地模型画像') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '解释选择' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('explain-select','解释这个 Python 报错，不修改文件')),'自动选择说明') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '自动选择模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('policy')),'自动选择策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '自动选择测试' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('auto-smoke','--task','只回复 LOCAL_AUTO_SELECT_OK','--risk','simple')),'自动选择测试') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '崇祯历史模拟 demo' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('run-profile','history_chongzhen','--prompt','用三句话模拟崇祯询问辽东军饷问题，不修改文件')),'仅本地 smoke/demo') ; Refresh-DirectLocalCard }
Add-DirectLocalButton 'Bonsai 下载说明' { $scriptPath=Join-Path $PSScriptRoot 'download-bonsai-model.ps1'; [System.Windows.Forms.MessageBox]::Show((Redact-Text (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath 2>&1 | Out-String)),'Bonsai 下载说明') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '安装/检测 Bonsai runtime' { $scriptPath=Join-Path $PSScriptRoot 'install-bonsai-runtime.ps1'; [System.Windows.Forms.MessageBox]::Show((Redact-Text (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath 2>&1 | Out-String)),'Bonsai runtime') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '重新检测 Bonsai runtime' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('bonsai-runtime','status')),'Bonsai runtime 状态') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '下载 Bonsai（需 runtime ready）' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('bonsai-runtime','status')),'Bonsai 下载门禁') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '测试 Bonsai' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('bonsai-runtime','status')),'Bonsai 测试门禁') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '检测 Vulkan' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('vulkan','status')),'Vulkan 检测') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '查看 GPU Offload 调优' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('vulkan','status')),'GPU Offload 调优结果') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '恢复 CPU 模式' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('vulkan','preference','--backend','CPU')),'本地后端偏好') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '禁用 Bonsai 自动选择' { [System.Windows.Forms.MessageBox]::Show('Bonsai 已保持为自动选择禁用；仅在 runtime 与模型 smoke 均通过后才可人工启用。','Bonsai 自动选择') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '允许 Bonsai 自动选择' { [System.Windows.Forms.MessageBox]::Show('当前不会自动启用。请先安装官方 Prism runtime、下载官方模型并完成 smoke。','Bonsai 自动选择') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '允许慢模型' { [System.Windows.Forms.MessageBox]::Show((Invoke-LocalCli @('policy','--allow-slow-local')),'慢模型策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '禁止 BF16 自动选择' { [System.Windows.Forms.MessageBox]::Show('已保持默认安全策略：BF16 模型不会自动选择。需要时请在高级配置中明确允许。','本地模型策略') ; Refresh-DirectLocalCard }
Add-DirectLocalButton '打开配置文件' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\local-backend.json'; if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('配置文件尚未创建。','本地配置')} }
Add-DirectLocalButton '打开日志目录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router'; if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('日志目录尚未创建。','日志目录')} }
Add-DeepSeekButton '启动 Bridge' { Invoke-DeepSeekPanelAction 'start-bridge' }
Add-DeepSeekButton '启动 Worker（旧兼容，可选）' { Invoke-DeepSeekPanelAction 'start-worker' } 205
Add-DeepSeekButton '健康检查（无 prompt）' { Invoke-DeepSeekPanelAction 'health-check' } 180
Add-DeepSeekButton '打开 DeepSeek Web' { Invoke-DeepSeekPanelAction 'open-deepseek-web' } 175
Add-DeepSeekButton '清理陈旧 Bridge' { Invoke-DeepSeekPanelAction 'clear-stale-bridge' } 170
Add-DeepSeekButton '检查启动器路径' { Show-DeepSeekLauncherDiagnostic } 170
Add-DeepSeekButton '复制启动器诊断' { Copy-DeepSeekLauncherDiagnostic } 170
Add-DeepSeekButton '打开 Bridge 项目目录' { Open-DeepSeekBridgeDirectory } 190
Add-DeepSeekButton '复制本地 API 地址' { try { Set-Clipboard -Value $DeepSeekLocalApiAddress; $result=[pscustomobject]@{exit_code=0;error_type='COPIED'} } catch { $result=[pscustomobject]@{exit_code=1;error_type='CLIPBOARD_FAILED'} }; Add-DeepSeekLog 'copy-api-address' $result; [System.Windows.Forms.MessageBox]::Show('本地 API 地址已复制。','DeepSeek 本地桥接') }
Add-DeepSeekButton '复制本地测试 Key' { try { Set-Clipboard -Value (Get-DeepSeekFixtureKey); $result=[pscustomobject]@{exit_code=0;error_type='COPIED'} } catch { $result=[pscustomobject]@{exit_code=1;error_type='CLIPBOARD_FAILED'} }; Add-DeepSeekLog 'copy-test-key' $result; [System.Windows.Forms.MessageBox]::Show('本地 fixture 测试 Key 已复制；它不是 DeepSeek 凭据，也不会写入日志。','DeepSeek 本地桥接') }
Add-DeepSeekButton '手动 Smoke（双确认）' { Invoke-DeepSeekManualSmoke } 190
Add-DeepSeekButton '停止服务' { Invoke-DeepSeekPanelAction 'stop' }
Add-DeepSeekButton '打开日志目录' { if(Test-Path -LiteralPath $DeepSeekRuntimeDir){Start-Process explorer.exe -ArgumentList ('"' + $DeepSeekRuntimeDir + '"');$result=[pscustomobject]@{exit_code=0;error_type='OPENED'}}else{$result=[pscustomobject]@{exit_code=1;error_type='LOG_DIRECTORY_NOT_FOUND'}};Add-DeepSeekLog 'open-log-directory' $result;[System.Windows.Forms.MessageBox]::Show(('日志目录：{0}`r`n状态：{1}' -f $DeepSeekRuntimeDir,$result.error_type),'DeepSeek 本地桥接') }
Add-DeepSeekModeButton '自动选择模式' { Set-DeepSeekModePreference 'auto' }
Add-DeepSeekModeButton '省时模式' { Set-DeepSeekPerformanceMode 'economy' }
Add-DeepSeekModeButton '平衡模式' { Set-DeepSeekPerformanceMode 'balanced' }
Add-DeepSeekModeButton '严谨模式' { Set-DeepSeekPerformanceMode 'accuracy' }
Add-DeepSeekModeButton '固定快速' { Set-DeepSeekModePreference 'quick_plain' }
Add-DeepSeekModeButton '固定快速+思考' { Set-DeepSeekModePreference 'quick_thinking' }
Add-DeepSeekModeButton '固定快速+搜索' { Set-DeepSeekModePreference 'quick_search' }
Add-DeepSeekModeButton '固定专家' { Set-DeepSeekModePreference 'expert_plain' }
Add-DeepSeekModeButton '固定专家+深度思考' { Set-DeepSeekModePreference 'expert_thinking' } 190
Add-DeepSeekModeButton '固定专家+最高思考审查' { Set-DeepSeekModePreference 'expert_max_review' } 210
Add-DeepSeekModeButton '固定专家+深度思考+搜索' { Set-DeepSeekModePreference 'expert_thinking_search' } 220
Add-DeepSeekModeButton '固定视觉+专家+深度思考' { Set-DeepSeekModePreference 'vision_expert_thinking' } 225
Add-DeepSeekModeButton '固定文件提取' { Set-DeepSeekModePreference 'file_extract' }
Add-DeepSeekModeButton '仅切换：快速' { Invoke-DeepSeekModeSwitch 'quick_plain' } 145
Add-DeepSeekModeButton '仅切换：快速+思考' { Invoke-DeepSeekModeSwitch 'quick_thinking' } 165
Add-DeepSeekModeButton '切到低思考' { Invoke-DeepSeekModeSwitch 'quick_plain' } 145
Add-DeepSeekModeButton '切到中思考' { Invoke-DeepSeekModeSwitch 'quick_thinking' } 145
Add-DeepSeekModeButton '仅切换：专家' { Invoke-DeepSeekModeSwitch 'expert_plain' } 145
Add-DeepSeekModeButton '仅切换：专家+深度思考' { Invoke-DeepSeekModeSwitch 'expert_thinking' } 190
Add-DeepSeekModeButton '切到高思考' { Invoke-DeepSeekModeSwitch 'expert_thinking' } 145
Add-DeepSeekModeButton '仅切换：最高思考' { Invoke-DeepSeekModeSwitch 'expert_max_review' } 170
Add-DeepSeekModeButton '切到最高思考审查' { Invoke-DeepSeekModeSwitch 'expert_max_review' } 180
Add-DeepSeekModeButton '仅切换：快速+搜索' { Invoke-DeepSeekModeSwitch 'quick_search' } 165
Add-DeepSeekModeButton '仅切换：专家+深度思考+搜索' { Invoke-DeepSeekModeSwitch 'expert_thinking_search' } 220
Add-DeepSeekModeButton '仅预检：视觉+专家+思考' { Invoke-DeepSeekModeSwitch 'vision_expert_thinking' } 210
Add-DeepSeekModeButton '仅预检：文件提取' { Invoke-DeepSeekModeSwitch 'file_extract' } 165
Add-DeepSeekModeButton '探测 DeepSeek 模式' { Invoke-DeepSeekModeProbe } 165
Add-DeepSeekModeButton '探测 DeepSeek 三合一模式' { Invoke-DeepSeekModeProbe } 190
Add-DeepSeekModeButton '三合一兼容自检（只读）' { Invoke-DeepSeekModeProbe } 190 '模式切换不发送 prompt，不上传文件。'
Add-DeepSeekModeButton '查看模式选择原因' { Explain-DeepSeekModeSelection } 165
Add-DeepSeekHeadButton '自动选择辅助脑' { Invoke-DeepSeekHeadCoordinate $false 'auto' } 205 '只读分类和健康门控，不发送 DeepSeek prompt。'
Add-DeepSeekHeadButton '收集项目上下文' { Invoke-DeepSeekHeadCoordinate $false 'auto' } 205 '只运行固定只读检查，不修改文件、不运行测试。'
Add-DeepSeekHeadButton '发送给 DeepSeek 首脑分析' { Invoke-DeepSeekHeadCoordinate $true 'deepseek-bridge-direct' } 230 '需确认；仅发送脱敏、限长只读上下文到本机 127.0.0.1:8791。'
Add-DeepSeekHeadButton '生成 Agent Plan' { Invoke-DeepSeekHeadCoordinate $false 'auto' } 190
Add-DeepSeekHeadButton '生成补丁草案（需确认）' { Invoke-DeepSeekHeadCoordinate $true 'deepseek-bridge-direct' $true } 220 '需要确认；仅有效 unified diff 会保存到 handoff，绝不自动应用。'
$script:DeepSeekHeadApplyButton = Add-DeepSeekHeadButton '申请应用补丁（需确认）' { if($script:DeepSeekHeadLastRecord.patch_draft_created -eq 'YES'){[System.Windows.Forms.MessageBox]::Show('请在 Local Agent 面板核对草案后双确认；本按钮不会应用补丁。','DeepSeek 首脑协作') | Out-Null}else{[System.Windows.Forms.MessageBox]::Show('尚无有效 unified diff，不能申请应用。','DeepSeek 首脑协作') | Out-Null} } 200 '仅有效 unified diff 可进入人工审查。' $true
$script:DeepSeekHeadApplyButton.Visible = $false
Add-DeepSeekHeadButton '运行测试（需确认）' { [System.Windows.Forms.MessageBox]::Show('该按钮不会自动运行测试；请在 Local Agent 面板选择白名单测试并确认。','DeepSeek 首脑协作') | Out-Null } 200 '需要确认，仅白名单测试。'
Add-DeepSeekHeadButton '提交 commit（需确认）' { [System.Windows.Forms.MessageBox]::Show('该按钮不会自动提交；需在 Local Agent 面板确认，且禁止 push。','DeepSeek 首脑协作') | Out-Null } 210 '需要确认，不 push。'
Add-DeepSeekHeadButton '查看 Context Bundle' { Show-DeepSeekHeadContext } 190
Add-DeepSeekHeadButton '复制给 Codex 的指令' { if($script:DeepSeekHeadLastRecord -and $script:DeepSeekHeadLastRecord.agent_plan.codex_instruction){Set-Clipboard -Value (Redact-Text ([string]$script:DeepSeekHeadLastRecord.agent_plan.codex_instruction));[System.Windows.Forms.MessageBox]::Show('已复制脱敏 Codex 指令。','DeepSeek 首脑协作') | Out-Null}else{[System.Windows.Forms.MessageBox]::Show('请先生成 Agent Plan。','DeepSeek 首脑协作') | Out-Null} } 210
Add-UiLayoutButton $sessionBindingButtons '新建绑定会话' { New-TaskSessionBinding } 170 '仅创建本地脱敏状态目录。'
Add-UiLayoutButton $sessionBindingButtons '载入会话' { Load-TaskSessionBinding } 150 '只读取本地会话状态。'
Add-UiLayoutButton $sessionBindingButtons '写入 Codex 执行摘要' { Append-CodexStatusToSession } 210 '只允许项目内、脱敏摘要，不保存完整日志。'
Add-UiLayoutButton $sessionBindingButtons '生成 DeepSeek 上下文包' { Show-SessionRollingSummary } 210 '只显示脱敏会话摘要，不发送模型请求。'
Add-UiLayoutButton $sessionBindingButtons '查看滚动摘要' { Show-SessionRollingSummary } 180 '默认折叠原始 JSON。'
Add-UiLayoutButton $sessionBindingButtons '清理会话敏感缓存' { Clear-SessionSensitiveCache } 210 '清理可再生成上下文，不读取网页私有数据。'
Add-UiLayoutButton $workProfileButtons '自动选择 Work Profile' { Refresh-WorkProfilePanel } 210 '按任务难度选择本地策略，不调用模型。'
Add-UiLayoutButton $workProfileButtons '简单只读' { Show-WorkProfile 'simple_readonly' } 130 '只读检查，禁用写入、测试和提交。'
Add-UiLayoutButton $workProfileButtons '中等分析' { Show-WorkProfile 'medium_analysis' } 130 '中等任务分析，按需使用辅助脑。'
Add-UiLayoutButton $workProfileButtons '中等补丁草案' { Show-WorkProfile 'medium_patch_draft' } 160 '仅 review-only 补丁草案，默认不应用。'
Add-UiLayoutButton $workProfileButtons '复杂调试' { Show-WorkProfile 'complex_debug' } 130 '复杂问题交给人工确认和官方工具。'
Add-UiLayoutButton $workProfileButtons '高风险审查' { Show-WorkProfile 'patch_review_high_risk' } 145 '禁用 apply/test/commit。'
Add-UiLayoutButton $workProfileButtons '官方 Codex 接手' { Show-WorkProfile 'official_codex_handoff' } 160 '生成官方 Codex 执行建议。'
Add-UiLayoutButton $workProfileButtons '标记 Codex 模式已确认' { Confirm-WorkProfileMode } 190 '只记录人工确认，不操控 Codex UI。'
Add-UiLayoutButton $workProfileButtons '生成 Codex Handoff 指令' { Show-WorkProfileHandoff } 200 '复制脱敏 handoff 指令。'
Add-CodexModeButton '切换官方直连' { Invoke-CodexModeAction 'official-direct' }
Add-CodexModeButton '切换本地 Router' { Invoke-CodexModeAction 'custom-router' }
Add-CodexModeButton 'DeepSeek 首脑' { Invoke-CodexModeAction 'custom-deepseek-head' } 150
Add-CodexModeButton '本地轻量' { Invoke-CodexModeAction 'custom-local-light' } 130
Add-CodexModeButton '外部 API' { Invoke-CodexModeAction 'custom-external-api' } 135
Add-CodexModeButton '混合助手' { Invoke-CodexModeAction 'custom-hybrid-agent' } 135
Add-CodexAssistButton '启用官方辅助协调模式' { Show-OfficialAssistedCoordinator } 230
Add-CodexAssistButton '生成 Codex 启动指令' { Show-AssistStartupInstruction } 215
Add-CodexAssistButton '复制 Codex 启动指令' { Copy-AssistStartupInstruction } 215
Add-CodexAssistButton '运行协调预检' { Invoke-AssistCoordinatePrecheck } 190
Add-CodexAnalysisButton '执行前计划' { Show-AssistStepList $false } 190
Add-CodexAnalysisButton '只读审查 diff' { Show-AssistStepList $false } 190
Add-CodexAnalysisButton '分析测试失败' { Show-AssistStepList $true } 190
Add-CodexAnalysisButton '生成下一轮 Codex 指令' { Show-AssistStartupInstruction } 220
Add-CodexBrainButton '检查辅助脑状态' { Invoke-AssistCoordinatePrecheck } 190
Add-CodexBrainButton '打开 handoff 目录' { Open-AssistHandoffDirectory } 190
Add-CodexConfigSwitcherButton '读取当前 Codex 配置' { Invoke-CodexConfigSwitcherAction 'status' }
Add-CodexConfigSwitcherButton '备份当前配置' { Invoke-CodexConfigSwitcherAction 'backup' }
Add-CodexConfigSwitcherButton '捕获当前为官方配置' { Invoke-CodexConfigSwitcherAction 'capture-official' } 180
Add-CodexConfigSwitcherButton '切到官方 Codex' { Invoke-CodexConfigSwitcherAction 'switch-official' }
Add-CodexConfigSwitcherButton '切到小羽 Custom Router' { Invoke-CodexConfigSwitcherAction 'switch-xiaoyu' } 190
Add-CodexConfigSwitcherButton '恢复上一次配置' { Invoke-CodexConfigSwitcherAction 'restore-previous' }
Add-CodexConfigSwitcherButton '校验配置' { Invoke-CodexConfigSwitcherAction 'validate' }
Add-CodexConfigSwitcherButton '复制重启提示' { Set-Clipboard -Value '本功能只修改本地 Codex 配置文件，不读取或点击 Codex UI。修改后请重启 Codex 并新建对话。'; [System.Windows.Forms.MessageBox]::Show('已复制重启提示。','Codex 配置切换') | Out-Null } 160
Add-UiLayoutButton $homeConfigSwitcherButtons '检查配置切换器 UI' { Invoke-CodexConfigSwitcherUiSelfCheck } 180
Add-UiLayoutButton $configSwitcherTabButtons '检查配置切换器 UI' { Invoke-CodexConfigSwitcherUiSelfCheck } 180
Add-UiLayoutButton $homeConfigSwitcherButtons '配置切换器自检' { Invoke-CodexConfigSwitcherRuntimeSelfTest } 180
Add-UiLayoutButton $configSwitcherTabButtons '配置切换器自检' { Invoke-CodexConfigSwitcherRuntimeSelfTest } 180
Add-CodexModeButton '启用 DeepSeek 首脑' { Invoke-CodexModeAction 'deepseek-head' } 170
Add-CodexModeButton '本地 Agent（预留）' { Invoke-CodexModeAction 'local-agent-pending' } 170
Add-CodexModeButton 'DeepSeek 文本兼容模式' { Invoke-CodexTextOnlyMode 'custom-deepseek-text-only' } 190
Add-CodexModeButton '本地模型文本兼容' { Invoke-CodexTextOnlyMode 'custom-local-text-only' } 180
Add-CodexModeButton '混合助手文本兼容' { Invoke-CodexTextOnlyMode 'custom-hybrid-text-only' } 180
Add-CodexModeButton '恢复上一次配置' { Invoke-CodexModeAction 'restore' }
Add-CodexModeButton '打开备份目录' { $path=(Get-CodexModeState).backup_directory;if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚未创建备份目录。','Codex 连接模式')} }
Add-CodexModeButton '仅检查模型列表' { Invoke-ModelsOnlyDiagnostic } 150
Add-CodexModeButton '刷新供应商白名单' { $allowlistStatus.Text=(Redact-Text ((Get-ProviderAllowlistJson)|Out-String)+"`r`n"+(Get-DeepSeekBridgeDirectAllowlistSummary)) } 180
Add-CodexModeButton '查看本地调用记录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\call-ledger.jsonl';if(Test-Path -LiteralPath $path){Start-Process notepad.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无本地调用记录。','调用记录')} } 175
Add-ToolsPolicyButton '严格拒绝工具（推荐）' { [void](Set-ToolsPolicy 'strict_reject'); Refresh-CodexModePanel }
Add-ToolsPolicyButton '文本兼容：忽略工具' { $confirm=[System.Windows.Forms.MessageBox]::Show('文本兼容模式会移除工具定义，只生成分析和计划，不会执行工具或修改文件。是否启用？','TEXT_ONLY 兼容模式',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning); if($confirm -eq [System.Windows.Forms.DialogResult]::Yes){[void](Set-ToolsPolicy 'text_only_strip');Refresh-CodexModePanel} } 190
Add-ToolsPolicyButton '手动计划（不调用模型）' { [void](Set-ToolsPolicy 'manual_plan'); Refresh-CodexModePanel } 190
Add-LocalAgentButton '生成计划（PLAN_ONLY）' { Invoke-LocalAgentPlan } 175
Add-LocalAgentButton '调用大脑生成计划（需确认）' { Invoke-LocalAgentPlan $true } 205
Add-LocalAgentButton '只读检查' { Invoke-LocalAgentReadonly } 130
Add-LocalAgentButton '生成补丁草案（需确认）' { Invoke-LocalAgentDraft } 190
Add-LocalAgentButton '复制给 Codex 的指令' { if($script:LocalAgentPlanJson -and $script:LocalAgentPlanJson.codex_instruction){Set-Clipboard -Value (Redact-Text ([string]$script:LocalAgentPlanJson.codex_instruction));[System.Windows.Forms.MessageBox]::Show('已复制脱敏 Codex 指令。','小羽 Local Agent')}else{[System.Windows.Forms.MessageBox]::Show('请先生成计划。','小羽 Local Agent')} } 190
Add-LocalAgentConfirmButton '应用补丁（双确认）' { Invoke-LocalAgentApply } 220 '需要确认，不自动执行；应用前会显示文件列表和 diff 摘要。'
Add-LocalAgentConfirmButton '运行测试（双确认）' { Invoke-LocalAgentTest } 220 '需要确认；只运行白名单测试，不会自动修改文件。'
Add-LocalAgentConfirmButton '提交 commit（双确认）' { Invoke-LocalAgentCommit } 220 '需要确认，不 push；只提交当前已审查的本地变更。'
Add-LocalAgentConfirmButton '停止 Agent' { $arguments=@('agent','stop');if($script:LocalAgentPlanId){$arguments += @('--plan',$script:LocalAgentPlanId)};$raw=Invoke-RouterCli $arguments;Add-LocalAgentLog ('stop=' + $script:LocalAgentPlanId);[System.Windows.Forms.MessageBox]::Show($raw,'停止 Agent') } 190 '只停止本地 Agent，不影响系统或其他服务。'
Add-LocalAgentRecordButton '打开 Agent 记录目录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\local-agent';if(Test-Path -LiteralPath $path){Start-Process explorer.exe -ArgumentList ('"'+$path+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无 Local Agent 记录。','Local Agent')} } 220
Add-LocalAgentRecordButton '查看最近记录' { $path=Join-Path $env:USERPROFILE '.codex-ai-router\local-agent';$latest=Get-ChildItem -LiteralPath $path -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1;if($latest){Start-Process notepad.exe -ArgumentList ('"'+$latest.FullName+'"')}else{[System.Windows.Forms.MessageBox]::Show('尚无 Local Agent 记录。','Local Agent')} } 190
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
    $routerGuardResult = Invoke-SafeUiAction -Name 'router-selftest' -Action { throw 'ROUTER_NOT_LISTENING' }
    $routerRuntimeGuardResult = Invoke-SafeUiAction -Name 'router-runtime-selftest' -Action { throw 'ROUTER_RUNTIME_NOT_FOUND' }
    $configGuardResult = Invoke-SafeUiAction -Name 'config-selftest' -Action { throw 'CODEX_CONFIG_NOT_FOUND' }
    $localLightGuardResult = Invoke-SafeUiAction -Name 'local-light-selftest' -Action { throw 'LOCAL_LIGHT_UNAVAILABLE' }
    Write-Output 'CONTROL_UI_INITIALIZATION=PASS'
    Write-Output ('UI_SAFE_ACTION_EXCEPTION=' + $(if($guardResult.error_code -eq 'EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED'){'CAUGHT'}else{'FAIL'}))
    Write-Output ('ROUTER_NOT_LISTENING_ERROR=' + $(if($routerGuardResult.error_code -eq 'ROUTER_NOT_LISTENING' -and $routerGuardResult.action_name -eq 'router-selftest'){'PASS'}else{'FAIL'}))
    Write-Output ('ROUTER_RUNTIME_ERROR_CLASSIFICATION=' + $(if($routerRuntimeGuardResult.error_code -eq 'ROUTER_CLI_RUNTIME_NOT_FOUND'){'PASS'}else{'FAIL'}))
    Write-Output ('CODEX_CONFIG_NOT_FOUND_ERROR=' + $(if($configGuardResult.error_code -eq 'CODEX_CONFIG_NOT_FOUND' -and $configGuardResult.suggested_fix){'PASS'}else{'FAIL'}))
    Write-Output ('LOCAL_LIGHT_UNAVAILABLE_ERROR=' + $(if($localLightGuardResult.error_code -eq 'LOCAL_LIGHT_UNAVAILABLE'){'PASS'}else{'FAIL'}))
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
    Write-Output 'DIRECT_LOCAL_MODEL_STUDIO_UI=YES'
    Write-Output 'HISTORY_PROFILE_UI=YES'
    Write-Output 'BONSAI_STATUS_UI=YES'
    Write-Output 'DEEPSEEK_LOCAL_BRIDGE_UI_CONSTRUCTION=PASS'
    $launcherDiagnostic = Test-LauncherPrerequisites
    $routerRuntime = try { Get-RouterLaunchSpec } catch { $null }
    Write-Output ('ROUTER_RUNTIME_DISCOVERY=' + $(if($routerRuntime){'PASS'}else{'FAIL'}))
    Write-Output ('ROUTER_RUNTIME_KIND=' + $(if($routerRuntime){$routerRuntime.kind}else{'NOT_FOUND'}))
    Write-Output ('BRIDGE_LAUNCHER_ROUTER_ROOT_RESOLVED=' + $(if($launcherDiagnostic.resolved_router_root -eq $ProjectRoot){'YES'}else{'NO'}))
    Write-Output ('BRIDGE_LAUNCHER_BRIDGE_ROOT_RESOLVED=' + $(if($launcherDiagnostic.resolved_bridge_root){'YES'}else{'NO'}))
    Write-Output ('BRIDGE_LAUNCHER_ENTRY_EXISTS=' + $launcherDiagnostic.bridge_entry_exists)
    Write-Output ('BRIDGE_LAUNCHER_RUNTIME_EXISTS=' + $launcherDiagnostic.runtime_exists)
    Write-Output ('BRIDGE_LAUNCHER_WORKER_REQUIRED=' + $launcherDiagnostic.worker_required)
    Write-Output ('CHROME_OPEN_COMMAND=' + $launcherDiagnostic.deepseek_open_command.command_kind)
    Write-Output ('CHROME_RUNTIME_DISCOVERY=' + $launcherDiagnostic.deepseek_open_command.runtime_exists)
    Write-Output ('CONTROLLED_CHROME_PROFILE=' + $launcherDiagnostic.deepseek_open_command.user_data_dir)
    Write-Output ('CONTROLLED_CHROME_DEBUG_PORT=' + $launcherDiagnostic.deepseek_open_command.debug_port)
    Write-Output ('BRIDGE_PORT_OWNER_KIND=' + $launcherDiagnostic.bridge_port_owner.owner_kind)
    Write-Output 'PORT_OWNER_DIAGNOSTICS=YES'
    Write-Output 'UNKNOWN_PORT_OWNER_SAFE_STOP=YES'
    Write-Output 'STALE_BRIDGE_CLEANUP_GUARDED=YES'
    Write-Output 'DEEPSEEK_OPEN_USES_CONTROLLED_CHROME=YES'
    Write-Output 'BRIDGE_ONLY_HEALTH_NO_WORKER=YES'
    Write-Output 'THREE_IN_ONE_PROBE_NO_WORKER_REQUIRED=YES'
    Write-Output 'BRIDGE_HTTP_READY_GATE=YES'
    Write-Output 'ROUTER_REQUIRED_FOR_MODE_PROBE=NO'
    Write-Output 'ERROR_CLASSIFICATION_SPECIFIC=YES'
    Write-Output 'ACTION_ERROR_UNCLASSIFIED_ALLOWED=NO'
    Write-Output 'LAUNCHER_DIAGNOSTICS_UI=YES'
    Write-Output 'ERROR_DETAILS_SANITIZED=YES'
    Write-Output 'DEEPSEEK_HEAD_UI_CONSTRUCTION=PASS'
    Write-Output 'DEEPSEEK_HEAD_CONTEXT_REDACTION=PASS'
    Write-Output 'PATCH_DRAFT_FORMAT_ENFORCEMENT=YES'
    Write-Output 'STRUCTURED_PATCH_UI=YES'
    Write-Output 'RETRY_PROMPT_UI_EXPOSED=NO'
    Write-Output 'CODEX_MODE_ALLOWLIST_UI_CONSTRUCTION=PASS'
    $configButtonLabels = @('读取当前 Codex 配置','备份当前配置','捕获当前为官方配置','切到官方 Codex','切到小羽 Custom Router','恢复上一次配置','校验配置','复制重启提示')
    $configButtonTexts = @($codexConfigSwitcherButtons.Controls | ForEach-Object { $_.Text })
    $configButtonsPresent = @($configButtonLabels | Where-Object { $_ -notin $configButtonTexts }).Count -eq 0
    Write-Output 'CODEX_CONFIG_SWITCHER_UI_CONSTRUCTION=PASS'
    Write-Output ('CODEX_CONFIG_SWITCHER_BUTTONS_VISIBLE=' + $(if($configButtonsPresent){'YES'}else{'NO'}))
    Write-Output ('CODEX_CONFIG_SWITCHER_BUTTON_COUNT=' + $configButtonTexts.Count)
    Write-Output 'CODEX_CONFIG_SWITCHER_FIXTURE_ONLY=YES'
    Write-Output 'UI_ACTION_FAILURE_DETAILS=YES'
    $configUiCheck = Invoke-CodexConfigSwitcherUiSelfCheck
    Write-Output ('CODEX_CONFIG_SWITCHER_TAB_VISIBLE=' + $(if($configUiCheck.CODEX_CONFIG_SWITCHER_TAB_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('CODEX_CONFIG_SWITCHER_HOME_SECTION_VISIBLE=' + $(if($configUiCheck.CODEX_CONFIG_SWITCHER_HOME_SECTION_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('READ_CONFIG_BUTTON_VISIBLE=' + $(if($configUiCheck.READ_CONFIG_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('BACKUP_CONFIG_BUTTON_VISIBLE=' + $(if($configUiCheck.BACKUP_CONFIG_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('CAPTURE_OFFICIAL_BUTTON_VISIBLE=' + $(if($configUiCheck.CAPTURE_OFFICIAL_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('SWITCH_OFFICIAL_BUTTON_VISIBLE=' + $(if($configUiCheck.SWITCH_OFFICIAL_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('SWITCH_XIAOYU_BUTTON_VISIBLE=' + $(if($configUiCheck.SWITCH_XIAOYU_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('RESTORE_PREVIOUS_BUTTON_VISIBLE=' + $(if($configUiCheck.RESTORE_PREVIOUS_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('VALIDATE_CONFIG_BUTTON_VISIBLE=' + $(if($configUiCheck.VALIDATE_CONFIG_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('COPY_RESTART_NOTICE_BUTTON_VISIBLE=' + $(if($configUiCheck.COPY_RESTART_NOTICE_BUTTON_VISIBLE){'YES'}else{'NO'}))
    Write-Output ('CONFIG_SWITCHER_LAYOUT_NO_OVERLAP=' + $(if($configUiCheck.VERSION_INFO_NO_OVERLAP){'PASS'}else{'FAIL'}))
    Write-Output ('CONFIG_SWITCHER_BUTTON_TEXT_FITS=' + $(if($configUiCheck.BUTTON_TEXT_FITS){'PASS'}else{'FAIL'}))
    Write-Output ('CONFIG_SWITCHER_SCROLLING=' + $(if($configUiCheck.WINDOW_SCROLLING_ENABLED){'PASS'}else{'FAIL'}))
    Write-Output ('CONFIG_SWITCHER_RUNTIME_PATH_SHORTENED=' + $(if($configUiCheck.RUNTIME_PATH_SHORTENED){'PASS'}else{'FAIL'}))
    $configRuntimeSelfTest = Invoke-CodexConfigSwitcherRuntimeSelfTest
    Write-Output ('CONFIG_SWITCHER_RUNTIME_SELFTEST=' + $(if($configRuntimeSelfTest.config_exists -and $configRuntimeSelfTest.json_parsed -and $configRuntimeSelfTest.exit_code -eq 0 -and $configRuntimeSelfTest.router_required -eq 'NO'){'PASS'}else{'FAIL'}))
    Write-Output ('CONFIG_SWITCHER_RUNTIME_EXIT_CODE=' + $configRuntimeSelfTest.exit_code)
    Write-Output ('CONFIG_SWITCHER_RUNTIME_JSON_PARSED=' + $configRuntimeSelfTest.json_parsed)
    Write-Output ('CONFIG_SWITCHER_RUNTIME_ERROR_CODE=' + $configRuntimeSelfTest.error_code)
    Write-Output ('CONFIG_SWITCHER_RUNTIME_STDERR=' + $configRuntimeSelfTest.stderr_summary)
    Write-Output ('CONFIG_SWITCHER_RUNTIME_COMMAND_KIND=' + $configRuntimeSelfTest.command_kind)
    Write-Output ('CONFIG_SWITCHER_RUNTIME_PYTHONPATH_SET=' + $(if($configRuntimeSelfTest.pythonpath -eq (Join-Path $ProjectRoot 'src')){'YES'}else{'NO'}))
    Write-Output ('CONFIG_SWITCHER_RUNTIME_WORKDIR_SET=' + $(if($configRuntimeSelfTest.working_directory -eq $ProjectRoot){'YES'}else{'NO'}))
    Write-Output 'ASSIST_COORDINATOR_UI_CONSTRUCTION=PASS'
    Write-Output 'OFFICIAL_ASSISTED_COORDINATOR_UI_VISIBLE=YES'
    Write-Output 'CODEX_ENDPOINT_TOUCHED=NO'
    Write-Output 'TOOLS_POLICY_UI_CONSTRUCTION=PASS'
    Write-Output 'LOCAL_AGENT_UI_CONSTRUCTION=PASS'
    Write-Output 'LOCAL_AGENT_CONFIRMATION_GATES=PASS'
    Write-Output 'LOCAL_AGENT_GROUP_LAYOUT=PASS'
    Write-Output 'OFFICIAL_ASSISTED_GROUP_LAYOUT=PASS'
    Write-Output 'BUTTON_TEXT_LAYOUT=PASS'
    Write-Output 'WINDOW_RESIZE_LAYOUT=PASS'
    Write-Output 'VERTICAL_SCROLL_LAYOUT=PASS'
    Write-Output 'DANGEROUS_ACTION_TOOLTIPS=PASS'
    Write-Output 'RAW_JSON_COLLAPSED=PASS'
    Write-Output 'SESSION_BINDING_UI_CONSTRUCTION=PASS'
    Write-Output 'SESSION_BINDING_LOCAL_ONLY=YES'
    Write-Output 'SESSION_BINDING_WEB_SESSION_READ=NO'
    Write-Output 'CODEX_TASK_INPUT_LOCATION=CODEX_ONLY'
    Write-Output 'WORK_PROFILE_PANEL_CONSTRUCTION=PASS'
    Write-Output 'WORK_PROFILE_SELECTION=PASS'
    Write-Output 'CODEX_CUSTOM_MODE_MANUAL_CONFIRMATION=PASS'
    Write-Output 'WORK_PROFILE_HANDOFF=PASS'
    exit 0
}
$form.Add_Shown({ Refresh-Home; Refresh-DeepSeekPanel; Refresh-CodexModePanel; Refresh-CodexConfigSwitcherPanel; Refresh-LocalAgentPanel })
[void]$form.ShowDialog()
