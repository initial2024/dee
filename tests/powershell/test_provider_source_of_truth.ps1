$ErrorActionPreference = 'Stop'
$script:passed = 0
function Assert-True { param([bool]$Condition, [string]$Name) if (-not $Condition) { throw "FAIL: $Name" }; $script:passed++ }

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$root = Join-Path ([System.IO.Path]::GetTempPath()) ('router-source-of-truth-' + [guid]::NewGuid().ToString('N'))
$path = Join-Path $root 'providers.json'
$originalConfig = $env:XIAOYU_ROUTER_PROVIDER_CONFIG
$originalPythonPath = $env:PYTHONPATH
[System.IO.Directory]::CreateDirectory($root) | Out-Null
try {
  $env:XIAOYU_ROUTER_PROVIDER_CONFIG = $path
  $env:PYTHONPATH = Join-Path $projectRoot 'src'
  . (Join-Path $projectRoot 'scripts\configure-provider.ps1') -NonInteractive

  Assert-True ((Get-CanonicalProviderConfigPath) -eq $path) 'default_config_path_same_between_ps_and_python'
  Set-ProviderConfiguration -ProviderId 'test-provider' -DisplayName 'Test Provider' -ProviderType 'openai_compatible' -BaseUrl 'http://127.0.0.1:1/v1' -WireApi 'responses' -ApiKeyEnvironmentName 'TEST_PROVIDER_KEY' -SkipEnvironmentUpdate
  $psRecord = (Get-HeaderMappingConfig $path)['providers']['test-provider']
  Assert-True ($psRecord['id'] -eq 'test-provider') 'provider_save_readback'

  $pythonIds = & py -3 -c "from codex_ai_router import provider_config; print(','.join(provider_config.load()['providers']))"
  Assert-True ($pythonIds -match 'test-provider') 'powershell_add_python_list'

  $cliList = (& xiaoyu-router provider list 2>&1 | Out-String | ConvertFrom-Json)
  Assert-True ((@($cliList.providers | ForEach-Object { $_.id }) -contains 'test-provider')) 'ps_to_cli_provider_list'
  $cliShow = (& xiaoyu-router provider show test-provider 2>&1 | Out-String | ConvertFrom-Json)
  Assert-True ($cliShow.id -eq 'test-provider') 'ps_to_cli_provider_show'
  $cliModels = (& xiaoyu-router provider models test-provider 2>&1 | Out-String)
  Assert-True ($cliModels -notmatch 'provider not found') 'ps_to_cli_provider_models_metadata_visible'

  & py -3 -c "from codex_ai_router import provider_config; provider_config.upsert('python-provider', {'display_name':'Python Provider','type':'openai_compatible','base_url':'http://127.0.0.1:1/v1','api_key_env':'PYTHON_PROVIDER_KEY'})"
  Assert-True ((Get-HeaderMappingConfig $path)['providers'].ContainsKey('python-provider')) 'python_add_powershell_read'

  Set-ProviderHeaderMapping -ProviderName 'test-provider' -HeaderName 'X-Selector' -HeaderEnvironmentName 'TEST_SELECTOR_ENV' -ConfigPath $path -SkipEnvironmentUpdate
  $headerEnv = & py -3 -c "from codex_ai_router import provider_config; print(provider_config.load()['providers']['test-provider']['headers']['X-Selector'])"
  Assert-True ($headerEnv -eq 'TEST_SELECTOR_ENV') 'header_update_visible_to_cli'

  $suggested = Get-SuggestedProviderId -BaseUrl 'https://test-provider.example' -Providers (Get-HeaderMappingConfig $path)['providers']
  Assert-True ($suggested -eq 'test-provider-2') 'collision_detection_same_store'

  $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
  Assert-True ($raw -notmatch 'secret-value|Bearer\s+|sk-') 'secret_not_in_metadata'
  "POWERSHELL_TEST_TOTAL=$script:passed"
  "POWERSHELL_TEST_PASS=$script:passed"
  'POWERSHELL_TEST_FAIL=0'
} finally {
  $env:XIAOYU_ROUTER_PROVIDER_CONFIG = $originalConfig
  $env:PYTHONPATH = $originalPythonPath
  if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
