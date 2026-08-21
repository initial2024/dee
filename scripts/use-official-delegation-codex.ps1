[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'use-openai-codex.ps1')
$instructionDir=Join-Path $root '.codex-ai-router';New-Item -ItemType Directory -Force -Path $instructionDir|Out-Null
$instruction=Join-Path $instructionDir 'official-delegation-instructions.md'
@'
# Official Codex Delegation Mode

Classify risk first. Delegate simple and medium advisory work with `scripts/xiaoyu-delegate.ps1`.
Review any draft before editing files. Keep complex and high-risk changes under official Codex control.
Delegation is read-only unless an explicit `-AllowWrite` flag is authorized. Never expose credentials.
This handoff does not copy official Codex cache; use handoff.md and local ledgers for continuity.
'@ | Set-Content -LiteralPath $instruction -Encoding UTF8
Write-Output 'OFFICIAL_DELEGATION_MODE=CONFIGURED'
Write-Output 'CODEX_DESKTOP_RESTART_MAY_BE_REQUIRED=YES'
