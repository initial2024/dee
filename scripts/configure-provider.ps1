param([switch]$NonInteractive, [switch]$Advanced, [string]$ConfigPath, [switch]$SkipEnvironmentUpdate)
$ErrorActionPreference = 'Stop'
if ([Console]::IsInputRedirected) { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false) }
$providerConfigurationNonInteractive = $NonInteractive
. (Join-Path $PSScriptRoot 'configure-provider-header.ps1') -NonInteractive
$NonInteractive = $providerConfigurationNonInteractive

function Get-SuggestedProviderId {
  param([string]$BaseUrl, [hashtable]$Providers)
  try { $hostName = ([uri]$BaseUrl).Host.ToLowerInvariant() } catch { throw 'Base URL is invalid.' }
  $known = @{ 'lightboat' = 'lightboat'; 'groq' = 'groq'; 'openrouter' = 'openrouter'; 'nvidia' = 'nvidia'; 'deepseek' = 'deepseek' }
  $suggestion = $null
  foreach ($name in $known.Keys) { if ($hostName -like "*$name*") { $suggestion = $known[$name]; break } }
  if ([string]::IsNullOrWhiteSpace($suggestion)) { $suggestion = ((($hostName -split '\.')[0..([Math]::Max(0, ($hostName -split '\.').Count - 2))] -join '-') -replace '[^a-z0-9_-]', '-') }
  if ([string]::IsNullOrWhiteSpace($suggestion)) { $suggestion = 'provider' }
  $suggestion = $suggestion.ToLowerInvariant()
  $candidate = $suggestion; $suffix = 2
  while ($Providers.ContainsKey($candidate)) { $candidate = "$suggestion-$suffix"; $suffix++ }
  return $candidate.ToLowerInvariant()
}

function Get-DefaultDisplayName {
  param([string]$ProviderId)
  return (($ProviderId -replace '[_-]+', ' ').Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries) | ForEach-Object {
    $_.Substring(0, 1).ToUpperInvariant() + $_.Substring(1)
  }) -join ' '
}

function Get-SuggestedProviderType {
  param([string]$BaseUrl)
  $uri = [uri]$BaseUrl
  if ($uri.Host -in @('localhost', '127.0.0.1') -and $uri.Port -eq 1234) { return 'lmstudio' }
  return 'openai_compatible'
}

function Get-SuggestedWireApi {
  param([string]$BaseUrl, [string]$ProviderType)
  if ($ProviderType -eq 'lmstudio') { return 'chat_completions' }
  $normalized = $BaseUrl.TrimEnd('/'); if ($normalized -notmatch '/v1$') { $normalized += '/v1' }
  foreach ($candidate in @(@{ Name = 'responses'; Url = "$normalized/responses" }, @{ Name = 'chat_completions'; Url = "$normalized/chat/completions" })) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -Method Options -Uri $candidate.Url -TimeoutSec 5
      if ($response.Headers['Content-Type'] -notmatch 'text/html') { return $candidate.Name }
    } catch {
      $http = $_.Exception.Response
      if ($null -ne $http -and $http.ContentType -notmatch 'text/html' -and [int]$http.StatusCode -in @(400, 401, 403, 405)) { return $candidate.Name }
    }
  }
  return 'responses'
}

function Set-ProviderConfiguration {
  param(
    [string]$ProviderId, [string]$ProviderType, [string]$BaseUrl, [string]$WireApi,
    [string]$ApiKeyEnvironmentName, [int]$Priority = 100,
    [string]$ConfigPath = (Get-CanonicalProviderConfigPath),
    [string]$DisplayName,
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
  $provider['id'] = $ProviderId; $provider['display_name'] = if ([string]::IsNullOrWhiteSpace($DisplayName)) { if ($provider.ContainsKey('display_name')) { $provider['display_name'] } else { $ProviderId } } else { $DisplayName }; $provider['type'] = $ProviderType; $provider['base_url'] = $BaseUrl
  $provider['wire_api'] = $WireApi; $provider['enabled'] = $true; $provider['api_key_env'] = $ApiKeyEnvironmentName
  $provider['model_discovery'] = $true; $provider['models'] = @(); $provider['priority'] = $Priority
  if (-not $provider.ContainsKey('headers')) { $provider['headers'] = @{} }
  Save-HeaderMappingConfigAtomic $config $ConfigPath
  $readback = Get-HeaderMappingConfig $ConfigPath
  if (-not $readback['providers'].ContainsKey($ProviderId) -or $readback['providers'][$ProviderId]['id'] -ne $ProviderId -or $readback['providers'][$ProviderId]['display_name'] -ne $provider['display_name']) { throw 'Provider metadata save/readback verification failed.' }
  if (-not $SkipEnvironmentUpdate) {
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $ProviderId, 'User')
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_BASE', $BaseUrl, 'User')
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_WIRE_API', $WireApi, 'User')
    if ($ProviderType -ne 'lmstudio') { [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY_ENV', $ApiKeyEnvironmentName, 'User') }
  }
}

if (-not $NonInteractive -and $MyInvocation.InvocationName -ne '.') {
  $baseUrl = (Read-Host 'Base URL').Trim()
  $activeConfigPath = if ([string]::IsNullOrWhiteSpace($ConfigPath)) { Get-CanonicalProviderConfigPath } else { $ConfigPath }
  $existingConfig = Get-HeaderMappingConfig $activeConfigPath
  $suggestedId = Get-SuggestedProviderId $baseUrl $existingConfig['providers']
  $providerId = if ($Advanced) { (Read-Host "Provider ID [$suggestedId]").Trim() } else { $suggestedId }
  if ([string]::IsNullOrWhiteSpace($providerId)) { $providerId = $suggestedId }
  $suggestedType = Get-SuggestedProviderType $baseUrl
  $providerType = if ($Advanced) { (Read-Host 'Provider type (openai_compatible, lmstudio, custom_openai_compatible)').Trim() } else { $suggestedType }
  Write-Host "Detected provider type: $providerType"
  Write-Host "Suggested Provider ID: $providerId"
  $defaultDisplayName = Get-DefaultDisplayName $providerId
  $displayName = (Read-Host "Display name [$defaultDisplayName]").Trim()
  if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = $defaultDisplayName }
  $suggestedWireApi = Get-SuggestedWireApi $baseUrl $providerType
  $wireApi = if ($Advanced) { (Read-Host 'Wire API (responses, chat_completions, auto_if_supported)').Trim() } else { $suggestedWireApi }
  $suggestedKeyEnv = 'XIAOYU_API_' + ($providerId.ToUpperInvariant() -replace '[^A-Z0-9]', '_') + '_KEY'
  $keyEnv = if ($providerType -eq 'lmstudio') { '' } elseif ($Advanced) { (Read-Host 'API key environment variable name').Trim() } else { $suggestedKeyEnv }
  Set-ProviderConfiguration -ProviderId $providerId -DisplayName $displayName -ProviderType $providerType -BaseUrl $baseUrl -WireApi $wireApi -ApiKeyEnvironmentName $keyEnv -ConfigPath $activeConfigPath -SkipEnvironmentUpdate:$SkipEnvironmentUpdate
  if ($providerType -ne 'lmstudio' -and -not $SkipEnvironmentUpdate) {
    $secureKey = Read-Host 'API key' -AsSecureString; $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { [Environment]::SetEnvironmentVariable($keyEnv, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr), 'User') }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }
  try {
    $modelsEndpoint = ($baseUrl.TrimEnd('/') + $(if ($baseUrl.TrimEnd('/') -match '/v1$') { '' } else { '/v1' }) + '/models')
    $response = Invoke-WebRequest -UseBasicParsing -Uri $modelsEndpoint -TimeoutSec 10
    if ($response.Headers['Content-Type'] -notmatch 'text/html') { $count = (($response.Content | ConvertFrom-Json).data | Measure-Object).Count; Write-Host "Discovered models: $count" }
  } catch { Write-Host 'Model discovery will run when the configured provider is available.' }
  while ((Read-Host 'Configure a custom header? (yes/no)').Trim().ToLowerInvariant() -eq 'yes') {
    $headerName = (Read-Host 'Header name').Trim(); $headerEnv = (Read-Host 'Header value environment variable name').Trim()
    $secureHeader = Read-Host 'Header value' -AsSecureString; $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureHeader)
    try { Set-ProviderHeaderMapping -ProviderName $providerId -HeaderName $headerName -HeaderEnvironmentName $headerEnv -ConfigPath $activeConfigPath -SkipEnvironmentUpdate:$SkipEnvironmentUpdate; if (-not $SkipEnvironmentUpdate) { [Environment]::SetEnvironmentVariable($headerEnv, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr), 'User') } }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }
  if ($SkipEnvironmentUpdate) { Write-Host 'Saved provider metadata to the supplied configuration path. No environment variables were written.' }
  else { Write-Host 'Saved provider metadata and sensitive values to current-user environment variables. No secret was displayed.' }
}
