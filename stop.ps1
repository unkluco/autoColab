param([string]$ConfigFile = '')
$taskMain = Join-Path $PSScriptRoot 'main.py'
$taskConfig = if ($ConfigFile) { (Resolve-Path -LiteralPath $ConfigFile).Path } else { Join-Path $PSScriptRoot 'config.toml' }
& python $taskMain --config $taskConfig --stop
exit $LASTEXITCODE
