$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\configure-provider-header.ps1') -NonInteractive

$script:passed = 0
function Assert-True { param([bool]$Condition, [string]$Name) if (-not $Condition) { throw "FAIL: $Name" }; $script:passed++ }

$root = Join-Path ([System.IO.Path]::GetTempPath()) ('router-header-test-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($root) | Out-Null
$path = Join-Path $root 'headers.json'
try {
  $empty = Get-HeaderMappingConfig $path
  Assert-True ($empty['providers'].Count -eq 0) 'empty_config'

  Set-ProviderHeaderMapping -ProviderName 'provider-one' -HeaderName 'X-One' -HeaderEnvironmentName 'HEADER_ONE' -ConfigPath $path -SkipEnvironmentUpdate
  $one = Get-HeaderMappingConfig $path
  Assert-True ($one['providers']['provider-one']['headers']['X-One'] -eq 'HEADER_ONE') 'powershell51_json_parse'
  Assert-True ($one['providers'].Count -eq 1) 'existing_provider_merge'

  Set-ProviderHeaderMapping -ProviderName 'provider-two' -HeaderName 'X-Two' -HeaderEnvironmentName 'HEADER_TWO' -ConfigPath $path -SkipEnvironmentUpdate
  $two = Get-HeaderMappingConfig $path
  Assert-True ($two['providers'].Count -eq 2 -and $two['providers']['provider-one']['headers'].ContainsKey('X-One')) 'multiple_provider_preserved'

  Set-ProviderHeaderMapping -ProviderName 'provider-one' -HeaderName 'X-One' -HeaderEnvironmentName 'HEADER_ONE_NEW' -ConfigPath $path -SkipEnvironmentUpdate
  $updated = Get-HeaderMappingConfig $path
  Assert-True ($updated['providers']['provider-one']['headers']['X-One'] -eq 'HEADER_ONE_NEW') 'existing_header_update'

  Set-ProviderHeaderMapping -ProviderName 'provider-one' -HeaderName 'X-Other' -HeaderEnvironmentName 'HEADER_OTHER' -ConfigPath $path -SkipEnvironmentUpdate
  $other = Get-HeaderMappingConfig $path
  Assert-True ($other['providers']['provider-one']['headers'].Count -eq 2) 'other_headers_preserved'
  $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
  Assert-True ($raw -notmatch 'secret-value') 'secret_not_written_to_json'
  Assert-True (@(Get-ChildItem -LiteralPath $root -Filter '*.tmp').Count -eq 0) 'atomic_write'

  [System.IO.File]::WriteAllText($path, '{bad json}', (New-Object System.Text.UTF8Encoding($false)))
  $safeError = $false
  try { Get-HeaderMappingConfig $path | Out-Null } catch { $safeError = $true }
  Assert-True $safeError 'malformed_json_safe_error'
  Assert-True ($updated['providers']['provider-two']['headers']['X-Two'] -eq 'HEADER_TWO') 'mapping_values_are_env_names_only'
  "POWERSHELL_TEST_TOTAL=$script:passed"
  "POWERSHELL_TEST_PASS=$script:passed"
  'POWERSHELL_TEST_FAIL=0'
} finally {
  if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
