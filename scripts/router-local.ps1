param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Task)
xiaoyu-router delegate --mode LOCAL_ONLY ($Task -join ' ')
