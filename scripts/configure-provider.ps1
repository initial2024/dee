param([switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'configure-provider-header.ps1') -NonInteractive

function Set-ProviderConfiguration {
  param(
    [string]$ProviderId, [string]$ProviderType, [string]$BaseUrl, [string]$WireApi,
    [string]$ApiKeyEnvironmentName, [int]$Priority = 100,
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex-ai-router\provider-header-mappings.json'),
    [switch]$SkipEnvironmentUpdate
  )
  if ($ProviderId -notmatch '^[a-z0-9_-]+$') { throw 'Provider ID must match [a-z0-9_-]+.' }
  if ($ProviderType -notin @('openai_compatible', 'custom_openai_compatible', 'lmstudio')) { throw 'Provider type is invalid.' }
  if ($WireApi -notin @('responses', 'chat_completions', 'auto_if_supported')) { throw 'Wire API is invalid.' }
  if ([string]::IsNullOrWhiteSpace($BaseUrl)) { throw 'Base URL is required.' }
  if ($ProviderType -ne 'lmstudio' -and $ApiKeyEnvironmentName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'API key environment variable name is invalid.' }
  $config = Get-HeaderMappingConfig $ConfigPath
  if (-not $config['providers'].ContainsKey($ProviderId)) { $config['providers'][$ProviderId] = @{ headers = @{} } }
  $provider = $config['providers'][$ProviderId]
  $provider['id'] = $ProviderId; $provider['type'] = $ProviderType; $provider['base_url'] = $BaseUrl
  $provider['wire_api'] = $WireApi; $provider['enabled'] = $true; $provider['api_key_env'] = $ApiKeyEnvironmentName
  $provider['model_discovery'] = $true; $provider['models'] = @(); $provider['priority'] = $Priority
  if (-not $provider.ContainsKey('headers')) { $provider['headers'] = @{} }
  Save-HeaderMappingConfigAtomic $config $ConfigPath
  if (-not $SkipEnvironmentUpdate) {
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $ProviderId, 'User')
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_BASE', $BaseUrl, 'User')
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_WIRE_API', $WireApi, 'User')
    if ($ProviderType -ne 'lmstudio') { [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY_ENV', $ApiKeyEnvironmentName, 'User') }
  }
}

if (-not $NonInteractive -and $MyInvocation.InvocationName -ne '.') {
  $providerId = (Read-Host 'Provider ID').Trim()
  $providerType = (Read-Host 'Provider type (openai_compatible, lmstudio, custom_openai_compatible)').Trim()
  $baseUrl = (Read-Host 'Base URL').Trim()
  $wireApi = (Read-Host 'Wire API (responses, chat_completions, auto_if_supported)').Trim()
  $keyEnv = (Read-Host 'API key environment variable name').Trim()
  Set-ProviderConfiguration -ProviderId $providerId -ProviderType $providerType -BaseUrl $baseUrl -WireApi $wireApi -ApiKeyEnvironmentName $keyEnv
  if ($providerType -ne 'lmstudio') {
    $secureKey = Read-Host 'API key' -AsSecureString; $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { [Environment]::SetEnvironmentVariable($keyEnv, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr), 'User') }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }
  while ((Read-Host 'Configure a custom header? (yes/no)').Trim().ToLowerInvariant() -eq 'yes') {
    $headerName = (Read-Host 'Header name').Trim(); $headerEnv = (Read-Host 'Header value environment variable name').Trim()
    $secureHeader = Read-Host 'Header value' -AsSecureString; $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureHeader)
    try { Set-ProviderHeaderMapping -ProviderName $providerId -HeaderName $headerName -HeaderEnvironmentName $headerEnv; [Environment]::SetEnvironmentVariable($headerEnv, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr), 'User') }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }
  Write-Host 'Saved provider metadata and sensitive values to current-user environment variables. No secret was displayed.'
}
