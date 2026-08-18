$ErrorActionPreference = 'Stop'

$provider = (Read-Host 'Provider name').Trim()
$headerName = (Read-Host 'Header name').Trim()
$headerEnv = (Read-Host 'Header value environment variable name').Trim()
if ([string]::IsNullOrWhiteSpace($provider)) { throw 'Provider name is required.' }
if ($headerName -notmatch '^[A-Za-z0-9-]+$') { throw 'Header name is invalid.' }
if ($headerEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Header environment variable name is invalid.' }

$secureValue = Read-Host 'Header value' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
try {
  $plainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  $existing = [Environment]::GetEnvironmentVariable('XIAOYU_CODER_API_HEADER_ENVS_JSON', 'User')
  $mapping = if ([string]::IsNullOrWhiteSpace($existing)) { @{} } else { $existing | ConvertFrom-Json -AsHashtable }
  $mapping[$headerName] = $headerEnv
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_PROVIDER', $provider, 'User')
  [Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_HEADER_ENVS_JSON', ($mapping | ConvertTo-Json -Compress), 'User')
  [Environment]::SetEnvironmentVariable($headerEnv, $plainValue, 'User')
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

Write-Host 'Saved the header environment-variable mapping and value for the current user. The header value was not displayed.'
