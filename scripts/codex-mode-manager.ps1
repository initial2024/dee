[CmdletBinding()]
param(
  [ValidateSet('status','official-direct','custom-router','custom-deepseek-head','custom-local-light','custom-external-api','custom-hybrid-agent','official-assisted','deepseek-head','local-agent-pending','restore')][string]$Action = 'status',
  [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
  [string]$StateRoot = (Join-Path $env:USERPROFILE '.codex-ai-router\codex-mode')
)

$ErrorActionPreference = 'Stop'
$LocalEndpoint = 'http://127.0.0.1:8792/v1'
$RouterEndpoint = 'http://127.0.0.1:18789/v1'
$ProviderId = 'XiaoyuLocalDeepSeek'

function Assert-CleanText([string[]]$Lines) { $markers=@([string][char]0x951B,[string][char]0x9286,[string][char]0x6DC7,[string][char]0x6D63); foreach($marker in $markers){if(($Lines -join "`n").Contains($marker)){throw 'MOJIBAKE_GUARD'}} }
function Read-State { $path=Join-Path $StateRoot 'state.json'; if(Test-Path -LiteralPath $path){return (Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json)}; return [pscustomobject]@{mode='UNKNOWN';official_baseline='';last_backup=''} }
function Write-State($Value) { New-Item -ItemType Directory -Force -Path $StateRoot|Out-Null; [IO.File]::WriteAllText((Join-Path $StateRoot 'state.json'),($Value|ConvertTo-Json -Depth 4),(New-Object Text.UTF8Encoding($false))) }
function Save-Utf8Atomic([string[]]$Lines) { $temp=$ConfigPath+'.xiaoyu-mode.tmp'; [IO.File]::WriteAllText($temp,(($Lines -join "`n")+"`n"),(New-Object Text.UTF8Encoding($false))); Move-Item -LiteralPath $temp -Destination $ConfigPath -Force }
function Backup-Config([object]$State) { New-Item -ItemType Directory -Force -Path $StateRoot|Out-Null; $backup=Join-Path $StateRoot ('config-'+(Get-Date -Format 'yyyyMMdd-HHmmss-fff')+'.toml'); Copy-Item -LiteralPath $ConfigPath -Destination $backup -Force; if(-not $State.official_baseline){Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $StateRoot 'official-baseline.toml') -Force;$State.official_baseline=Join-Path $StateRoot 'official-baseline.toml'}; $State.last_backup=$backup; return $backup }
function Set-TopLevelString([System.Collections.Generic.List[string]]$Lines,[string]$Name,[string]$Value){$sectionAt=$Lines.Count;for($i=0;$i -lt $Lines.Count;$i++){if($Lines[$i] -match '^\s*\['){$sectionAt=$i;break}};for($i=0;$i -lt $sectionAt;$i++){if($Lines[$i] -match ('^\s*'+[regex]::Escape($Name)+'\s*=')){$Lines[$i]=$Name+' = "'+$Value+'"';return}};$Lines.Insert($sectionAt,$Name+' = "'+$Value+'"')}
function Set-ProviderString([System.Collections.Generic.List[string]]$Lines,[string]$Name,[string]$Value,[string]$ProviderSection=$ProviderId){$header='[model_providers.'+$ProviderSection+']';$start=-1;$end=$Lines.Count;for($i=0;$i -lt $Lines.Count;$i++){if($Lines[$i] -eq $header){$start=$i;continue};if($start -ge 0 -and $Lines[$i] -match '^\s*\['){$end=$i;break}};if($start -lt 0){$Lines.Add('');$Lines.Add($header);$start=$Lines.Count-1;$end=$Lines.Count};for($i=$start+1;$i -lt $end;$i++){if($Lines[$i] -match ('^\s*'+[regex]::Escape($Name)+'\s*=')){$Lines[$i]=$Name+' = "'+$Value+'"';return}};$Lines.Insert($end,$Name+' = "'+$Value+'"')}
function Set-ProviderBoolean([System.Collections.Generic.List[string]]$Lines,[string]$Name,[bool]$Value,[string]$ProviderSection=$ProviderId){$header='[model_providers.'+$ProviderSection+']';$start=-1;$end=$Lines.Count;for($i=0;$i -lt $Lines.Count;$i++){if($Lines[$i] -eq $header){$start=$i;continue};if($start -ge 0 -and $Lines[$i] -match '^\s*\['){$end=$i;break}};if($start -lt 0){$Lines.Add('');$Lines.Add($header);$start=$Lines.Count-1;$end=$Lines.Count};$literal=if($Value){'true'}else{'false'};for($i=$start+1;$i -lt $end;$i++){if($Lines[$i] -match ('^\s*'+[regex]::Escape($Name)+'\s*=')){$Lines[$i]=$Name+' = '+$literal;return}};$Lines.Insert($end,$Name+' = '+$literal)}
function Get-ConfigSummary([object]$State){$lines=if(Test-Path -LiteralPath $ConfigPath){Get-Content -LiteralPath $ConfigPath -Encoding UTF8}else{@()};$provider='DEFAULT';$model='DEFAULT';foreach($line in $lines){if($line -match '^\s*\['){break};if($line -match '^\s*model_provider\s*=\s*"([^"]*)"'){$provider=$Matches[1]};if($line -match '^\s*model\s*=\s*"([^"]*)"'){$model=$Matches[1]}};$joined=$lines -join "`n";$endpoint=if($model -in @('deepseek-head','deepseek-web','deepseek-web-search')){'LOCAL_8792'}elseif($model -eq 'local-light'){'LOCAL_MODEL_1234'}elseif($model -in @('external-fast','external-strong','hybrid-agent')){'LOCAL_ROUTER_18789'}else{'OFFICIAL_OR_UNMANAGED'};[ordered]@{mode=[string]$State.mode;provider=$provider;model=$model;endpoint=$endpoint;no_quota_mode=[bool]$State.no_quota_mode;backup_directory=$StateRoot;env_mutation='NONE'}}

function Set-CustomMode([System.Collections.Generic.List[string]]$list,[string]$ModeName,[string]$ModeProvider,[string]$ModeModel,[string]$BaseUrl){
  if(-not $state.PSObject.Properties['no_quota_mode']){$state|Add-Member -NotePropertyName no_quota_mode -NotePropertyValue $false}; [void](Backup-Config $state); Set-TopLevelString $list 'model_provider' $ModeProvider; Set-TopLevelString $list 'model' $ModeModel; Set-ProviderString $list 'name' $ModeName $ModeProvider; Set-ProviderString $list 'base_url' $BaseUrl $ModeProvider; Set-ProviderString $list 'wire_api' 'chat_completions' $ModeProvider; Set-ProviderBoolean $list 'requires_openai_auth' $false $ModeProvider; $state.mode=$ModeName; $state.no_quota_mode=$true
}

if(-not(Test-Path -LiteralPath $ConfigPath)){throw 'CODEX_CONFIG_NOT_FOUND'}
$state=Read-State
if($Action -eq 'status'){Get-ConfigSummary $state|ConvertTo-Json -Compress;exit 0}
$lines=Get-Content -LiteralPath $ConfigPath -Encoding UTF8;Assert-CleanText $lines
switch($Action){
 'custom-router' { $list=[System.Collections.Generic.List[string]]::new([string[]]$lines);Set-CustomMode $list 'CUSTOM_ROUTER' 'XiaoyuLocalDeepSeek' 'deepseek-web' $LocalEndpoint;Save-Utf8Atomic $list }
 'custom-deepseek-head' { $list=[System.Collections.Generic.List[string]]::new([string[]]$lines);Set-CustomMode $list 'CUSTOM_DEEPSEEK_HEAD' 'XiaoyuLocalDeepSeekHead' 'deepseek-head' $LocalEndpoint;Save-Utf8Atomic $list }
 'custom-local-light' { $list=[System.Collections.Generic.List[string]]::new([string[]]$lines);$localEndpoint=if($env:XIAOYU_LOCAL_MODEL_BASE){$env:XIAOYU_LOCAL_MODEL_BASE}else{'http://127.0.0.1:1234/v1'};Set-CustomMode $list 'CUSTOM_LOCAL_LIGHT' 'XiaoyuLocalLight' 'local-light' $localEndpoint;Save-Utf8Atomic $list }
 'custom-external-api' { $list=[System.Collections.Generic.List[string]]::new([string[]]$lines);Set-CustomMode $list 'CUSTOM_EXTERNAL_API' 'XiaoyuRouterExternal' 'external-fast' $RouterEndpoint;Save-Utf8Atomic $list }
 'custom-hybrid-agent' { $list=[System.Collections.Generic.List[string]]::new([string[]]$lines);Set-CustomMode $list 'CUSTOM_HYBRID_AGENT' 'XiaoyuRouterHybrid' 'hybrid-agent' $RouterEndpoint;Save-Utf8Atomic $list }
 'official-direct' { if(-not $state.PSObject.Properties['no_quota_mode']){$state|Add-Member -NotePropertyName no_quota_mode -NotePropertyValue $false};[void](Backup-Config $state);$baseline=Join-Path $StateRoot 'official-baseline.toml';if(-not(Test-Path -LiteralPath $baseline)){throw 'OFFICIAL_BASELINE_MISSING'};Copy-Item -LiteralPath $baseline -Destination $ConfigPath -Force;$state.mode='OFFICIAL_DIRECT';$state.no_quota_mode=$false }
 'official-assisted' { $state.mode='OFFICIAL_ASSISTED' }
 'deepseek-head' { $state.mode='DEEPSEEK_HEAD' }
 'local-agent-pending' { $state.mode='LOCAL_AGENT_PENDING' }
 'restore' {if(-not $state.last_backup -or -not(Test-Path -LiteralPath $state.last_backup)){throw 'BACKUP_NOT_FOUND'};Copy-Item -LiteralPath $state.last_backup -Destination $ConfigPath -Force;$state.mode='UNKNOWN'}
}
Write-State $state;Get-ConfigSummary $state|ConvertTo-Json -Compress
