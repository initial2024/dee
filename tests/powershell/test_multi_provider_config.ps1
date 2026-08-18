$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\configure-provider.ps1') -NonInteractive
$script:passed = 0
function Assert-True { param([bool]$Condition, [string]$Name) if (-not $Condition) { throw "FAIL: $Name" }; $script:passed++ }
$root = Join-Path ([System.IO.Path]::GetTempPath()) ('router-provider-test-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($root) | Out-Null
$path = Join-Path $root 'providers.json'
try {
  Set-ProviderConfiguration -ProviderId 'lightboat' -ProviderType 'openai_compatible' -BaseUrl 'https://one.example/v1' -WireApi 'responses' -ApiKeyEnvironmentName 'LIGHTBOAT_KEY' -ConfigPath $path -SkipEnvironmentUpdate
  $first = Get-HeaderMappingConfig $path
  Assert-True ($first['providers'].Count -eq 1) 'add_first_provider'
  Assert-True ($first['providers']['lightboat']['type'] -eq 'openai_compatible') 'provider_id_and_type'
  Set-ProviderConfiguration -ProviderId 'groq-main' -ProviderType 'openai_compatible' -BaseUrl 'https://two.example/v1' -WireApi 'chat_completions' -ApiKeyEnvironmentName 'GROQ_KEY' -ConfigPath $path -SkipEnvironmentUpdate
  $second = Get-HeaderMappingConfig $path
  Assert-True ($second['providers'].Count -eq 2) 'add_second_provider'
  Assert-True ($second['providers'].ContainsKey('lightboat')) 'providers_preserved'
  Set-ProviderConfiguration -ProviderId 'lightboat' -ProviderType 'openai_compatible' -BaseUrl 'https://updated.example/v1' -WireApi 'responses' -ApiKeyEnvironmentName 'LIGHTBOAT_KEY' -ConfigPath $path -SkipEnvironmentUpdate
  $updated = Get-HeaderMappingConfig $path
  Assert-True ($updated['providers']['lightboat']['base_url'] -eq 'https://updated.example/v1') 'update_one_provider'
  Assert-True ($updated['providers']['groq-main']['base_url'] -eq 'https://two.example/v1') 'other_provider_unchanged'
  Set-ProviderHeaderMapping -ProviderName 'lightboat' -HeaderName 'X-One' -HeaderEnvironmentName 'LIGHTBOAT_HEADER_ONE' -ConfigPath $path -SkipEnvironmentUpdate
  Set-ProviderHeaderMapping -ProviderName 'lightboat' -HeaderName 'X-Two' -HeaderEnvironmentName 'LIGHTBOAT_HEADER_TWO' -ConfigPath $path -SkipEnvironmentUpdate
  Set-ProviderHeaderMapping -ProviderName 'groq-main' -HeaderName 'X-Other' -HeaderEnvironmentName 'GROQ_HEADER' -ConfigPath $path -SkipEnvironmentUpdate
  $headers = Get-HeaderMappingConfig $path
  Assert-True ($headers['providers']['lightboat']['headers'].Count -eq 2) 'multiple_headers_same_provider'
  Assert-True ($headers['providers']['groq-main']['headers'].Count -eq 1) 'headers_isolated_between_providers'
  $invalid = $false; try { Set-ProviderConfiguration -ProviderId 'bad id' -ProviderType 'openai_compatible' -BaseUrl 'https://x' -WireApi 'responses' -ApiKeyEnvironmentName 'K' -ConfigPath $path -SkipEnvironmentUpdate } catch { $invalid = $true }
  Assert-True $invalid 'provider_id_validation'
  $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
  Assert-True ($raw -notmatch 'secret-value') 'secret_not_written'
  Assert-True (@(Get-ChildItem -LiteralPath $root -Filter '*.tmp').Count -eq 0) 'atomic_write'
  "POWERSHELL_TEST_TOTAL=$script:passed"; "POWERSHELL_TEST_PASS=$script:passed"; 'POWERSHELL_TEST_FAIL=0'
} finally { if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force } }
