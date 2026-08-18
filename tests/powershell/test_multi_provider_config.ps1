$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\configure-provider.ps1') -NonInteractive
$script:passed = 0
function Assert-True { param([bool]$Condition, [string]$Name) if (-not $Condition) { throw "FAIL: $Name" }; $script:passed++ }
$root = Join-Path ([System.IO.Path]::GetTempPath()) ('router-provider-test-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($root) | Out-Null
$path = Join-Path $root 'providers.json'
try {
  $providerScript = (Resolve-Path (Join-Path $PSScriptRoot '..\..\scripts\configure-provider.ps1')).Path
  $processInfo = New-Object System.Diagnostics.ProcessStartInfo
  $processInfo.FileName = 'powershell.exe'
  $processInfo.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$providerScript`""
  $processInfo.UseShellExecute = $false
  $processInfo.RedirectStandardInput = $true
  $processInfo.RedirectStandardOutput = $true
  $processInfo.RedirectStandardError = $true
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $processInfo
  [void]$process.Start()
  $process.StandardInput.WriteLine('')
  $process.StandardInput.Close()
  $interactiveOutput = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
  $process.WaitForExit()
  Assert-True ($interactiveOutput -match 'Base URL is invalid.') 'default_interactive_starts_base_url'

  $generatedId = Get-SuggestedProviderId -BaseUrl 'https://lightboat.dpdns.org' -Providers @{}
  Assert-True ($generatedId -eq 'lightboat') 'generated_id_is_lowercase'
  Assert-True ($generatedId -match '^[a-z0-9_-]+$') 'generated_id_matches_validation'

  $chineseDisplayName = [string]::Concat(([char]0x8F7B), ([char]0x821F), ([char]0x516C), ([char]0x76CA), ([char]0x7AD9))
  Set-ProviderConfiguration -ProviderId 'lightboat' -DisplayName $chineseDisplayName -ProviderType 'openai_compatible' -BaseUrl 'https://one.example/v1' -WireApi 'responses' -ApiKeyEnvironmentName 'LIGHTBOAT_KEY' -ConfigPath $path -SkipEnvironmentUpdate
  $first = Get-HeaderMappingConfig $path
  Assert-True ($first['providers'].Count -eq 1) 'add_first_provider'
  Assert-True ($first['providers']['lightboat']['type'] -eq 'openai_compatible') 'provider_id_and_type'
  Assert-True ($first['providers']['lightboat']['display_name'] -eq $chineseDisplayName) 'chinese_display_name_roundtrip'
  Set-ProviderConfiguration -ProviderId 'groq-main' -ProviderType 'openai_compatible' -BaseUrl 'https://two.example/v1' -WireApi 'chat_completions' -ApiKeyEnvironmentName 'GROQ_KEY' -ConfigPath $path -SkipEnvironmentUpdate
  $second = Get-HeaderMappingConfig $path
  Assert-True ($second['providers'].Count -eq 2) 'add_second_provider'
  Assert-True ($second['providers'].ContainsKey('lightboat')) 'providers_preserved'
  $second['providers']['groq-main']['display_name'] = $chineseDisplayName
  Save-HeaderMappingConfigAtomic $second $path
  Assert-True ((Get-HeaderMappingConfig $path)['providers']['groq-main']['display_name'] -eq $chineseDisplayName) 'duplicate_display_name_allowed'
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
