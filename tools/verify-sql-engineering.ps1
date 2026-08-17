[CmdletBinding()]
param([string]$Root = (Join-Path $PSScriptRoot ".."))

$ErrorActionPreference = "Stop"
$rootFull = (Resolve-Path -LiteralPath $Root).Path
$python = (Get-Command python -ErrorAction Stop).Source

& $python -m compileall -q (Join-Path $rootFull "sql-engineering\scripts") (Join-Path $rootFull "setup\scripts")
if ($LASTEXITCODE -ne 0) { throw "Python syntax check failed." }

& $python (Join-Path $rootFull "tools\public_release.py") validate --root $rootFull
if ($LASTEXITCODE -ne 0) { throw "Public release validation failed." }

Write-Output "Public SQL Engineering checks passed."
