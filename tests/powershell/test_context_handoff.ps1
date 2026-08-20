$ErrorActionPreference = 'Stop'
$passed = 0; $failed = 0
function Assert-True([bool]$Value, [string]$Name) { if ($Value) { $script:passed++ } else { $script:failed++; Write-Output "FAILED=$Name" } }

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$make = Join-Path $root 'scripts\make-codex-handoff.ps1'
$switch = Join-Path $root 'scripts\switch-codex-with-handoff.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('xiaoyu-handoff-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $config = Join-Path $temp 'config.toml'
  $handoff = Join-Path $temp 'handoff.md'
  [IO.File]::WriteAllText($config, "model = `"gpt-5.6-terra`"`nmodel_reasoning_effort = `"medium`"`n[model_providers.XiaoyuRouter]`nbase_url = `"http://127.0.0.1:18789/v1`"`nwire_api = `"responses`"`n", (New-Object Text.UTF8Encoding($false)))
  $makeOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $make -ProjectRoot $root -OutputPath $handoff -Task 'handoff Bearer fake' -CompletedSteps 'unit test' -TestsRun 'PowerShell' -TestResult 'PASS' 2>&1
  $text = Get-Content -LiteralPath $handoff -Raw -Encoding UTF8
  Assert-True (($makeOutput -join "`n") -match 'HANDOFF_SECRET_FREE=YES') 'handoff_marks_secret_free'
  Assert-True ($text -match 'Git HEAD:') 'handoff_includes_git_head'
  Assert-True ($text -match '## Modified files') 'handoff_includes_modified_files'
  Assert-True ($text -match 'Do not repeat completed steps') 'handoff_includes_new_thread_template'
  Assert-True ($text -notmatch 'Bearer fake') 'handoff_redacts_secret_text'
  $xiaoyu = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $switch -ToXiaoyu -ConfigPath $config -ProjectRoot $root -HandoffPath $handoff 2>&1
  $xiaoyuConfig = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True (($xiaoyu -join "`n") -match 'SWITCH_TARGET=XIAOYU_ROUTER') 'switch_to_xiaoyu_after_handoff'
  Assert-True ($xiaoyuConfig -match '(?m)^model = "xiaoyu-auto"') 'stable_outer_model_is_xiaoyu_auto'
  $openai = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $switch -ToOpenAI -ModelProfile light -ConfigPath $config -ProjectRoot $root -HandoffPath $handoff 2>&1
  $openaiConfig = Get-Content -LiteralPath $config -Raw -Encoding UTF8
  Assert-True (($openai -join "`n") -match 'SWITCH_TARGET=OPENAI') 'switch_to_openai_after_handoff'
  Assert-True ($openaiConfig -match '(?m)^model = "gpt-5.6-luna"') 'light_profile_uses_luna'
  $makeSource = Get-Content -LiteralPath $make -Raw -Encoding UTF8
  $switchSource = Get-Content -LiteralPath $switch -Raw -Encoding UTF8
  Assert-True (($makeSource -notmatch '(?i)(Get-Content|Get-ChildItem).*(cookie|token)') -and ($switchSource -notmatch '(?i)(Get-Content|Get-ChildItem).*(cookie|token|cache)')) 'no_cookie_token_or_official_cache_access'
} finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }

Write-Output 'POWERSHELL_TEST_TOTAL=10'
Write-Output "POWERSHELL_TEST_PASS=$passed"
Write-Output "POWERSHELL_TEST_FAIL=$failed"
if ($failed -gt 0) { exit 1 }
