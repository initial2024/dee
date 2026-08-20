[CmdletBinding()]
param(
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$RouterBaseUrl = 'http://127.0.0.1:18789/v1'
)

$ErrorActionPreference = 'Stop'
function Get-TopLevelValue([string[]]$Lines, [string]$Name) {
  foreach ($line in $Lines) { if ($line -match '^\s*\[') { break }; if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*"([^"]*)"')) { return $Matches[1] } }
  return 'DEFAULT'
}
$lines = Get-Content -LiteralPath $ConfigPath -Encoding UTF8
Write-Output ('ACTIVE_PROVIDER=' + (Get-TopLevelValue $lines 'model_provider'))
Write-Output ('ACTIVE_MODEL=' + (Get-TopLevelValue $lines 'model'))
try {
  $health = Invoke-WebRequest -UseBasicParsing -Uri ($RouterBaseUrl.TrimEnd('/v1') + '/health') -TimeoutSec 3
  Write-Output ('ROUTER_REACHABLE=' + $(if ($health.StatusCode -eq 200) {'YES'} else {'NO'}))
  $models = Invoke-WebRequest -UseBasicParsing -Uri ($RouterBaseUrl + '/models') -TimeoutSec 3
  $parsed = $models.Content | ConvertFrom-Json
  Write-Output ('ROUTER_MODELS_AVAILABLE=' + $(if (@($parsed.data).Count -gt 0) {'YES'} else {'NO'}))
} catch {
  Write-Output 'ROUTER_REACHABLE=NO'
  Write-Output 'ROUTER_MODELS_AVAILABLE=NO'
}
