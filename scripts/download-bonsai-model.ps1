[CmdletBinding()]
param(
  [switch]$ConfirmDownload,
  [switch]$PreDownloadTestGatePass,
  [switch]$PrismRuntimeReady,
  [switch]$OfficialPrismMLRepo
)

$runtimeReady = $PreDownloadTestGatePass -and $PrismRuntimeReady -and $OfficialPrismMLRepo
$notice = [ordered]@{
  BONSAI_DOWNLOAD_HELPER = 'YES'
  BONSAI_AUTO_DOWNLOAD = 'NO'
  BONSAI_DOWNLOAD_REQUIRES_CONFIRM = 'YES'
  BONSAI_RUNTIME_WARNING_VISIBLE = 'YES'
  BONSAI_DOWNLOAD_REQUIRES_RUNTIME_READY = 'YES'
  PRE_DOWNLOAD_TEST_GATE = if ($PreDownloadTestGatePass) { 'PASS' } else { 'NOT_VERIFIED' }
  PRISM_BONSAI_RUNTIME_COMPATIBILITY = if ($PrismRuntimeReady) { 'PASS' } else { 'BONSAI_RUNTIME_UNKNOWN' }
  OFFICIAL_PRISMML_REPO_SELECTED = if ($OfficialPrismMLRepo) { 'YES' } else { 'NO' }
  status = if (-not $runtimeReady) { 'BONSAI_DOWNLOAD_BLOCKED_RUNTIME_NOT_READY' } elseif ($ConfirmDownload) { 'CONFIRMED_COMMANDS_ONLY' } else { 'CONFIRMATION_REQUIRED' }
  warning = 'Bonsai 2 27B PQ2_0/PTQ1_0 may require a PrismML-compatible llama.cpp build. Downloading a GGUF does not prove this llama-server can load it.'
  next_step = if (-not $runtimeReady) { 'Do not download. First pass the test gate, confirm the dedicated Prism runtime, and select an official PrismML repository.' } elseif ($ConfirmDownload) { 'Review the official PrismML model source and run its documented download command manually. This helper intentionally does not contain or execute a download URL.' } else { 'Re-run with -ConfirmDownload only after all runtime gates pass. No model download has started.' }
}
[pscustomobject]$notice | ConvertTo-Json -Depth 3
