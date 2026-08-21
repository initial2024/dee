[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-openai-codex.ps1')
$instructionDir=Join-Path $root '.codex-ai-router';New-Item -ItemType Directory -Force -Path $instructionDir|Out-Null
$instruction=Join-Path $instructionDir 'official-delegation-instructions.md'
@'
# Official Codex Delegation Mode

Classify risk before delegation. For simple or medium advisory work, run `scripts/xiaoyu-delegate.ps1` and integrate its JSON result. Official Codex reviews and performs file changes. Complex and high-risk work remains under official Codex control. Delegation is read-only unless explicitly authorized. Handoff and local ledgers preserve continuity; they do not copy official internal cache.
'@ | ForEach-Object { [IO.File]::WriteAllText($instruction, ($_ + "`n"), (New-Object Text.UTF8Encoding($false))) }
Write-Output 'OFFICIAL_DELEGATION_MODE=CONFIGURED'
Write-Output 'CODEX_DESKTOP_RESTART_MAY_BE_REQUIRED=YES'
