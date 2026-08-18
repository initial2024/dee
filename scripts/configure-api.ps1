$ErrorActionPreference = 'Stop'

$provider = (Read-Host 'Provider name').Trim()
$baseUrl = (Read-Host 'OpenAI-compatible Base URL').Trim()
$wireApi = (Read-Host 'Wire API (chat_completions or responses; default responses)').Trim()
$preferredModel = (Read-Host 'Optional preferred model; leave blank for automatic discovery').Trim()
$keyEnv = (Read-Host 'API Key environment variable name (for example XIAOYU_API_PROVIDER_KEY)').Trim()
$headerEnvJson = (Read-Host 'Optional JSON header-to-environment-variable mapping; leave blank for none').Trim()
if ([string]::IsNullOrWhiteSpace($provider) -or [string]::IsNullOrWhiteSpace($baseUrl)) { throw 'Provider and Base URL are required.' }
if ([string]::IsNullOrWhiteSpace($wireApi)) { $wireApi = 'responses' }
if ($wireApi -notin @('chat_completions', 'responses')) { throw 'Wire API must be chat_completions or responses.' }
if ($keyEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'API key environment variable name is invalid.' }
if (-not [string]::IsNullOrWhiteSpace($headerEnvJson)) {
  try { $headerMap = $headerEnvJson | ConvertFrom-Json -AsHashtable } catch { throw 'Header mapping must be a JSON object.' }
  foreach ($entry in $headerMap.GetEnumerator()) { if ($entry.Key -notmatch '^[A-Za-z0-9-]+$' -or $entry.Value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Header mapping contains an invalid header or environment variable name.' } }
}

$apiKey = Read-Host 'API Key' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKey)
try {
  $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $provider, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_BASE', $baseUrl, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_WIRE_API', $wireApi, 'User')
  if (-not [string]::IsNullOrWhiteSpace($preferredModel)) { [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_MODEL', $preferredModel, 'User') }
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY_ENV', $keyEnv, 'User')
  if (-not [string]::IsNullOrWhiteSpace($headerEnvJson)) { [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_HEADER_ENVS_JSON', $headerEnvJson, 'User') }
  [Environment]::SetEnvironmentVariable($keyEnv, $plainKey, 'User')
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

Write-Host 'Saved provider metadata and the API key to current-user environment variables. The API key was not displayed.'
