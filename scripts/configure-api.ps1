$ErrorActionPreference = 'Stop'

$provider = (Read-Host 'Provider name').Trim()
$baseUrl = (Read-Host 'OpenAI-compatible Base URL').Trim()
$model = (Read-Host 'Model').Trim()
$keyEnv = (Read-Host 'API Key environment variable name (for example XIAOYU_API_PROVIDER_KEY)').Trim()
if ([string]::IsNullOrWhiteSpace($provider) -or [string]::IsNullOrWhiteSpace($baseUrl) -or [string]::IsNullOrWhiteSpace($model)) { throw 'Provider, Base URL, and Model are required.' }
if ($keyEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'API key environment variable name is invalid.' }

$apiKey = Read-Host 'API Key' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKey)
try {
  $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $provider, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_BASE', $baseUrl, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_MODEL', $model, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY_ENV', $keyEnv, 'User')
  [Environment]::SetEnvironmentVariable($keyEnv, $plainKey, 'User')
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

Write-Host 'Saved provider metadata and the API key to current-user environment variables. The API key was not displayed.'
