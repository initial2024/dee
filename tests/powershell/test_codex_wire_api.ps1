$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-wire-api-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $config = Join-Path $temp 'config.toml'
  $stateRoot = Join-Path $temp 'state'
  $fixture = @'
model = "gpt-test"
[model_providers.XiaoyuLocalDeepSeekHead]
base_url = "http://127.0.0.1:8792/v1"
wire_api = "responses_shell"
api_key_env = "XIAOYU_TEST_KEY"

[model_providers.XiaoyuLocalLight]
base_url = "http://127.0.0.1:1234/v1"
wire_api = "chat_completions"

[model_providers.XiaoyuRouterHybrid]
base_url = "http://127.0.0.1:18789/v1"
custom_header_name = "Authorization"
'@
  [IO.File]::WriteAllText($config, $fixture, (New-Object Text.UTF8Encoding($false)))
  $repair = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\codex-mode-manager.ps1') -Action repair-wire-api -ConfigPath $config -StateRoot $stateRoot 2>&1
  $repaired = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True ($repaired -notmatch 'wire_api = "chat_completions"') 'repair_removes_chat_completions'
  Assert-True ($repaired -notmatch 'wire_api = "responses_shell"') 'repair_removes_responses_shell'
  Assert-True (($repaired | Select-String -AllMatches 'wire_api = "responses"').Matches.Count -eq 3) 'repair_sets_responses_for_all_xiaoyu_providers'
  Assert-True ((Get-ChildItem -LiteralPath $stateRoot -Filter 'config-*.toml').Count -ge 1) 'repair_creates_backup'
  $repairText = $repair -join "`n"
  Assert-True ($repairText -notmatch 'XIAOYU_TEST_KEY|Authorization') 'repair_output_redacts_key_and_authorization'

  foreach ($entry in @(
    @{ action = 'custom-router'; provider = 'XiaoyuLocalDeepSeek' },
    @{ action = 'custom-deepseek-head'; provider = 'XiaoyuLocalDeepSeekHead' },
    @{ action = 'custom-local-light'; provider = 'XiaoyuLocalLight' },
    @{ action = 'custom-external-api'; provider = 'XiaoyuRouterExternal' },
    @{ action = 'custom-hybrid-agent'; provider = 'XiaoyuRouterHybrid' }
  )) {
    $null = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\codex-mode-manager.ps1') -Action $entry.action -ConfigPath $config -StateRoot $stateRoot 2>&1
    $active = Get-Content -LiteralPath $config -Raw -Encoding UTF8
    $section = [regex]::Escape('[model_providers.' + $entry.provider + ']')
    Assert-True ($active -match ($section + '[\s\S]*?wire_api = "responses"')) ($entry.action + '_writes_responses')
  }

  $null = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\codex-mode-manager.ps1') -Action official-direct -ConfigPath $config -StateRoot $stateRoot 2>&1
  $official = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True (($official -notmatch 'wire_api = "chat_completions"') -and ($official -notmatch 'wire_api = "responses_shell"')) 'post_switch_repair_keeps_responses_only'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }
Write-Output 'POWERSHELL_TEST_TOTAL=11'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
