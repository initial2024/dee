[CmdletBinding()]
param(
  [switch]$Install
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'tools\prism-bonsai-runtime'
$serverCandidates = @(
  (Join-Path $runtimePath 'llama-server.exe'),
  (Join-Path $runtimePath 'bin\llama-server.exe')
)
$serverPath = @($serverCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1)

function Write-Result([hashtable]$Result) {
  [pscustomobject]$Result | ConvertTo-Json -Depth 5
}

function New-BaseResult {
  return [ordered]@{
    BONSAI_RUNTIME_INSTALLER = 'YES'
    OFFICIAL_PRISMML_RUNTIME_ONLY = 'YES'
    STANDARD_RUNTIME_NOT_REPLACED = 'YES'
    NO_IMPLICIT_RUNTIME_BUILD = 'YES'
    runtime_path = $runtimePath
    llama_server_path = if ($serverPath.Count) { [string]$serverPath[0] } else { $null }
    official_source = 'https://github.com/PrismML-Eng/Bonsai-demo'
    source_repository = 'https://github.com/PrismML-Eng/llama.cpp'
  }
}

if ($serverPath.Count) {
  $result = New-BaseResult
  $result.status = 'BONSAI_RUNTIME_ALREADY_INSTALLED'
  $result.BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND = 'NO'
  Write-Result $result
  exit 0
}

if (-not $Install) {
  $result = New-BaseResult
  $result.status = 'BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND'
  $result.BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND = 'YES'
  $result.BONSAI_RUNTIME_BUILD_REQUIRED = 'YES'
  $result.build_requirements = @('Visual Studio Build Tools with C++ workload', 'CMake', 'CUDA toolkit for CUDA builds or CPU-only CMake build')
  $result.build_estimate = 'Build time depends on the machine; no build was started.'
  $result.next_step = 'Obtain an official PrismML Windows release binary, or explicitly authorize a local build.'
  Write-Result $result
  exit 0
}

# Installation is deliberately limited to the official PrismML GitHub release API.
# It never invokes Bonsai-demo setup scripts because those scripts download models.
try {
  $headers = @{ 'User-Agent' = 'codex-ai-router-bonsai-runtime-installer' }
  $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/PrismML-Eng/llama.cpp/releases/latest' -Headers $headers
  $asset = @($release.assets | Where-Object {
    $_.browser_download_url -match '^https://github\.com/PrismML-Eng/llama\.cpp/releases/' -and
    $_.name -match '(?i)windows.*(cpu|cuda|vulkan|hip).*\.zip$'
  } | Select-Object -First 1)
  if (-not $asset.Count) {
    $result = New-BaseResult
    $result.status = 'BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND'
    $result.BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND = 'YES'
    $result.BONSAI_RUNTIME_BUILD_REQUIRED = 'YES'
    Write-Result $result
    exit 0
  }
  $staging = Join-Path ([System.IO.Path]::GetTempPath()) ('prism-bonsai-runtime-' + [guid]::NewGuid().ToString('N'))
  $zip = Join-Path $staging 'runtime.zip'
  New-Item -ItemType Directory -Path $staging -Force | Out-Null
  Invoke-WebRequest -Uri $asset[0].browser_download_url -Headers $headers -OutFile $zip
  Expand-Archive -LiteralPath $zip -DestinationPath $staging -Force
  $binary = Get-ChildItem -LiteralPath $staging -Filter 'llama-server.exe' -File -Recurse | Select-Object -First 1
  if (-not $binary) { throw 'BONSAI_RUNTIME_BINARY_MISSING_FROM_OFFICIAL_ASSET' }
  New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
  Copy-Item -LiteralPath $binary.FullName -Destination (Join-Path $runtimePath 'llama-server.exe') -Force
  $marker = [ordered]@{
    runtime_id = 'prism_bonsai'
    official_source = 'https://github.com/PrismML-Eng/llama.cpp'
    asset_url = [string]$asset[0].browser_download_url
    supported_formats = @('PQ2_0', 'PTQ1_0', 'Q2_0_G64')
  }
  [System.IO.File]::WriteAllText((Join-Path $runtimePath 'prism-bonsai-runtime.json'), ($marker | ConvertTo-Json -Depth 3), [System.Text.UTF8Encoding]::new($false))
  $result = New-BaseResult
  $result.status = 'BONSAI_RUNTIME_INSTALLED_OFFICIAL_BINARY'
  $result.llama_server_path = Join-Path $runtimePath 'llama-server.exe'
  $result.BONSAI_RUNTIME_WINDOWS_BINARY_NOT_FOUND = 'NO'
  Write-Result $result
} catch {
  $result = New-BaseResult
  $result.status = 'BONSAI_RUNTIME_INSTALL_FAILED'
  $result.error_code = 'BONSAI_RUNTIME_OFFICIAL_INSTALL_FAILED'
  Write-Result $result
  exit 1
}
