$ErrorActionPreference = 'Stop'
$base = Read-Host 'OpenAI-compatible Base URL'
$model = Read-Host 'Model'
$key = Read-Host 'API Key' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key)
try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_BASE', $base, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_MODEL', $model, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY', $plain, 'User')
} finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
Write-Host 'Saved user environment variables. API key was not displayed. Remove with [Environment]::SetEnvironmentVariable(name, $null, User).'
