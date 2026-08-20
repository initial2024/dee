[CmdletBinding()]
param(
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$Task = 'Continue the current Codex task',
  [string]$CompletedSteps = 'See current git diff and completed test records.',
  [string]$TestsRun = 'NOT_RUN',
  [string]$TestResult = 'NOT_RUN',
  [string]$Blockers = 'NONE',
  [string]$Constraints = 'Do not expose credentials. Do not repeat completed work.',
  [string]$NextRecommendedAction = 'Review this handoff, then continue from the current working tree.',
  [string]$OutputPath = ''
)

$ErrorActionPreference = 'Stop'

function Redact-Text([string]$Text) {
  return ($Text -replace '(?i)(bearer\s+)[^\s]+','$1[REDACTED]' -replace '(?i)(sk-[a-z0-9_-]+)','[REDACTED]' -replace '(?i)(authorization\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?i)((?:api[_ -]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+','$1[REDACTED]' -replace '(?s)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----','[REDACTED_PRIVATE_KEY]')
}
function Get-Git([string[]]$Arguments) {
  $result = & git -C $ProjectRoot @Arguments 2>$null
  if ($LASTEXITCODE -ne 0) { return 'UNKNOWN' }
  return ($result | Out-String).TrimEnd()
}

$root = [IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) { throw 'ProjectRoot must be a Git repository.' }
if ([string]::IsNullOrWhiteSpace($OutputPath)) { $OutputPath = Join-Path $root '.codex-ai-router\handoff.md' }
$output = [IO.Path]::GetFullPath($OutputPath)
$directory = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }

$head = Get-Git @('rev-parse','HEAD')
$status = Get-Git @('status','--short')
$modified = @()
if (-not [string]::IsNullOrWhiteSpace($status) -and $status -ne 'UNKNOWN') {
  foreach ($line in $status -split "`r?`n") { if ($line.Length -gt 3) { $modified += $line.Substring(3) } }
}
if ($modified.Count -eq 0) { $modified = @('NONE') }

$content = @(
  '# Codex Handoff',
  '',
  'Use this handoff in a new Codex thread. Do not repeat completed steps.',
  '',
  '## Project state',
  ('- Repo path: ' + (Redact-Text $root)),
  ('- Git HEAD: ' + (Redact-Text $head)),
  ('- Git status summary: ' + $(if ($status) { 'CHANGES_PRESENT' } else { 'CLEAN' })),
  '',
  '## Task',
  (Redact-Text $Task),
  '',
  '## Completed steps',
  (Redact-Text $CompletedSteps),
  '',
  '## Modified files'
)
foreach ($file in $modified) { $content += ('- ' + (Redact-Text $file)) }
$content += @(
  '',
  '## Validation',
  ('- Tests run: ' + (Redact-Text $TestsRun)),
  ('- Test result: ' + (Redact-Text $TestResult)),
  '',
  '## Blockers',
  (Redact-Text $Blockers),
  '',
  '## Constraints',
  (Redact-Text $Constraints),
  '',
  '## Next recommended action',
  (Redact-Text $NextRecommendedAction),
  '',
  '## New-thread template',
  'Please continue based on the following handoff. Do not repeat completed steps.',
  ''
)

$temp = $output + '.tmp'
[IO.File]::WriteAllText($temp, ($content -join "`n"), (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $temp -Destination $output -Force
Write-Output ('HANDOFF_PATH=' + $output)
Write-Output ('GIT_HEAD=' + $head)
Write-Output ('MODIFIED_FILE_COUNT=' + $modified.Count)
Write-Output 'HANDOFF_SECRET_FREE=YES'
