param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Task)
xiaoyu-router delegate --mode API_ONLY ($Task -join ' ')
