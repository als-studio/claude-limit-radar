# start-cc-usage.ps1 — сбор статистики расхода токенов Claude Code
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONUTF8 = "1"   # CLI печатает кириллицу — без этого Task Scheduler может
                        # подхватить неконсольную кодировку и уронить процесс
python "$root\cc_usage\cli.py" collect
