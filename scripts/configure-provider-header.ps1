param([switch]$NonInteractive)
$ErrorActionPreference = 'Stop'

function Get-CanonicalProviderConfigPath {
  if (-not [string]::IsNullOrWhiteSpace($env:XIAOYU_ROUTER_PROVIDER_CONFIG)) { return $env:XIAOYU_ROUTER_PROVIDER_CONFIG }
  return (Join-Path $env:USERPROFILE '.codex-ai-router\providers.json')
}

function ConvertTo-HashtableRecursive {
  param([Parameter(ValueFromPipeline=$true)]$Value)
  if ($null -eq $Value) { return $null }
  if ($Value -is [System.Collections.IDictionary]) {
    $result = @{}
    foreach ($key in $Value.Keys) { $result[[string]$key] = ConvertTo-HashtableRecursive $Value[$key] }
    return $result
  }
  if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
    return @($Value | ForEach-Object { ConvertTo-HashtableRecursive $_ })
  }
  if ($Value -is [pscustomobject]) {
    $result = @{}
    foreach ($property in $Value.PSObject.Properties) { $result[$property.Name] = ConvertTo-HashtableRecursive $property.Value }
    return $result
  }
  return $Value
}

function Get-HeaderMappingConfig {
  param([string]$ConfigPath)
  if (-not (Test-Path -LiteralPath $ConfigPath)) { return @{ providers = @{} } }
  try { $config = ConvertTo-HashtableRecursive (Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json) }
  catch { throw 'Existing provider header mapping JSON is malformed; no changes were made.' }
  if (-not ($config -is [hashtable])) { throw 'Existing provider header mapping must be a JSON object.' }
  if (-not $config.ContainsKey('providers')) { $config['providers'] = @{} }
  if (-not ($config['providers'] -is [hashtable])) { throw 'Existing providers mapping must be an object.' }
  return $config
}

function Save-HeaderMappingConfigAtomic {
  param([hashtable]$Config, [string]$ConfigPath)
  $directory = Split-Path -Parent $ConfigPath
  [System.IO.Directory]::CreateDirectory($directory) | Out-Null
  $temporary = Join-Path $directory ('.provider-headers-' + [guid]::NewGuid().ToString('N') + '.tmp')
  $json = $Config | ConvertTo-Json -Depth 12
  [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
  if (Test-Path -LiteralPath $ConfigPath) {
    $backup = $temporary + '.bak'
    [System.IO.File]::Replace($temporary, $ConfigPath, $backup)
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
  } else { Move-Item -LiteralPath $temporary -Destination $ConfigPath }
}

function Set-ProviderHeaderMapping {
  param(
    [string]$ProviderName,
    [string]$HeaderName,
    [string]$HeaderEnvironmentName,
    [string]$ConfigPath = (Get-CanonicalProviderConfigPath),
    [switch]$SkipEnvironmentUpdate
  )
  if ([string]::IsNullOrWhiteSpace($ProviderName)) { throw 'Provider name is required.' }
  if ($HeaderName -notmatch '^[A-Za-z0-9-]+$') { throw 'Header name is invalid.' }
  if ($HeaderEnvironmentName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Header environment variable name is invalid.' }
  $config = Get-HeaderMappingConfig $ConfigPath
  if (-not $config['providers'].ContainsKey($ProviderName)) { $config['providers'][$ProviderName] = @{ headers = @{} } }
  $provider = $config['providers'][$ProviderName]
  if (-not ($provider -is [hashtable])) { throw 'Existing provider mapping must be an object.' }
  if (-not $provider.ContainsKey('headers')) { $provider['headers'] = @{} }
  if (-not ($provider['headers'] -is [hashtable])) { throw 'Existing provider headers mapping must be an object.' }
  $provider['headers'][$HeaderName] = $HeaderEnvironmentName
  Save-HeaderMappingConfigAtomic $config $ConfigPath
  $readback = Get-HeaderMappingConfig $ConfigPath
  if (-not $readback['providers'].ContainsKey($ProviderName) -or $readback['providers'][$ProviderName]['headers'][$HeaderName] -ne $HeaderEnvironmentName) { throw 'Provider header mapping save/readback verification failed.' }
  if (-not $SkipEnvironmentUpdate) {
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $ProviderName, 'User')
    [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_HEADER_ENVS_JSON', ($provider['headers'] | ConvertTo-Json -Compress), 'User')
  }
}

if (-not $NonInteractive -and $MyInvocation.InvocationName -ne '.') {
  $provider = (Read-Host 'Provider name').Trim()
  $headerName = (Read-Host 'Header name').Trim()
  $headerEnv = (Read-Host 'Header value environment variable name').Trim()
  $secureValue = Read-Host 'Header value' -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
  try {
    $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    Set-ProviderHeaderMapping -ProviderName $provider -HeaderName $headerName -HeaderEnvironmentName $headerEnv
    [Environment]::SetEnvironmentVariable($headerEnv, $plainValue, 'User')
  } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  Write-Host 'Saved the mapping and header value to current-user environment variables. The header value was not displayed.'
}
