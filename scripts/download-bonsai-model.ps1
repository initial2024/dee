[CmdletBinding()]
param(
  [switch]$ConfirmDownload
)

$notice = [ordered]@{
  BONSAI_DOWNLOAD_HELPER = 'YES'
  BONSAI_AUTO_DOWNLOAD = 'NO'
  BONSAI_DOWNLOAD_REQUIRES_CONFIRM = 'YES'
  BONSAI_RUNTIME_WARNING_VISIBLE = 'YES'
  status = if ($ConfirmDownload) { 'CONFIRMED_COMMANDS_ONLY' } else { 'CONFIRMATION_REQUIRED' }
  warning = 'Bonsai 2 27B PQ2_0/PTQ1_0 may require a PrismML-compatible llama.cpp build. Downloading a GGUF does not prove this llama-server can load it.'
  next_step = if ($ConfirmDownload) { 'Review a trusted model source and run its documented download command manually. This helper intentionally does not contain or execute a download URL.' } else { 'Re-run with -ConfirmDownload to display the manual-download reminder. No model download has started.' }
}
[pscustomobject]$notice | ConvertTo-Json -Depth 3
