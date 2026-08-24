$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDirectory
python .\watcher.py --once
