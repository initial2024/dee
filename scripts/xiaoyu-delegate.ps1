[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Task,
  [ValidateSet('auto','read','review','plan')][string]$Mode='auto',
  [ValidateSet('auto','simple','medium','complex','high')][string]$Risk='auto',
  [ValidateRange(1,300)][int]$MaxSeconds=60,
  [switch]$AllowWrite
)
$ErrorActionPreference='Stop'
$homePath=Join-Path $env:USERPROFILE '.codex-ai-router'
$ledger=Join-Path $homePath 'delegation-ledger.jsonl'
function Out-SafeJson($value){$value|ConvertTo-Json -Depth 6 -Compress}
$started=[Diagnostics.Stopwatch]::StartNew();$success=$false;$errorCode=$null;$summary='';$route='OFFICIAL_CODEX';$provider=$null;$model=$null
try{
  $explainRaw=& xiaoyu-router explain-delegation --risk $Risk $Task 2>$null
  try{$explain=$explainRaw|ConvertFrom-Json}catch{throw 'Router did not return a valid policy response.'}
  $route=$explain.recommended_route
  if(-not $explain.delegation_allowed){$summary=$explain.why}
  else {
    $safeTask = if($AllowWrite){$Task}else{"Read-only advisory task. Do not edit files, run commands, or expose secrets. " + $Task}
    $job=Start-Job -ScriptBlock { param($prompt) & xiaoyu-router auto $prompt 2>&1 } -ArgumentList $safeTask
    if(Wait-Job -Job $job -Timeout $MaxSeconds){$result=Receive-Job -Job $job; $success=($job.State -eq 'Completed'); Remove-Job -Job $job -Force; try{$routerResult=($result|Out-String)|ConvertFrom-Json;$provider=$routerResult.provider;$model=$routerResult.model;$summary=[string]$routerResult.summary}catch{$summary='Router completed an advisory delegation.'}}
    else {Stop-Job -Job $job -ErrorAction SilentlyContinue;Remove-Job -Job $job -Force;$errorCode='ROUTER_TIMEOUT';$summary='Router delegation timed out; retain work in official Codex.'}
    if(-not $success -and -not $errorCode){$errorCode='ROUTER_COMMAND_FAILED'}
  }
  if(-not $explain.delegation_allowed){$success=$true}
} catch {$explain=[pscustomobject]@{risk='UNSAFE_OR_NEEDS_CONFIRMATION';requires_official_codex=$true};$route='OFFICIAL_CODEX';$errorCode='ROUTER_UNAVAILABLE';$summary='Router delegation unavailable; retain work in official Codex.'}
$payload=[ordered]@{ok=[bool]$success;risk=$explain.risk;route=$route;provider=$provider;model=$model;summary=$summary;suggested_actions=@();requires_official_codex=[bool]($explain.requires_official_codex -or -not $success);write_allowed=[bool]$AllowWrite;error_code=$errorCode}
New-Item -ItemType Directory -Force -Path $homePath|Out-Null
$entry=[ordered]@{timestamp=(Get-Date).ToUniversalTime().ToString('o');risk=$payload.risk;route=$payload.route;provider=$null;model=$null;duration_seconds=[math]::Round($started.Elapsed.TotalSeconds,3);success=$payload.ok;error_code=$payload.error_code;requires_official_codex=$payload.requires_official_codex;write_allowed=[bool]$AllowWrite}
[IO.File]::AppendAllText($ledger,((Out-SafeJson $entry)+"`n"),(New-Object Text.UTF8Encoding($false)))
Out-SafeJson $payload
