[CmdletBinding()]
param(
    [string]$Root = (Join-Path $PSScriptRoot ".."),
    [string]$ProjectRoot = "",
    [ValidateSet("text", "json")][string]$Format = "text"
)

$ErrorActionPreference = "Stop"
$rootFull = (Resolve-Path -LiteralPath $Root).Path
if (-not $ProjectRoot) { throw "Specify -ProjectRoot for a project health check." }
$projectFull = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = (Get-Command python -ErrorAction Stop).Source
$validator = Join-Path $rootFull "sql-engineering\scripts\project_validate.py"
if (-not (Test-Path -LiteralPath $validator)) { throw "project_validate.py is missing." }
$output = & $python $validator --root $projectFull --scope current --format json 2>&1
$code = $LASTEXITCODE
if ($Format -eq "json") { $output | ForEach-Object { "$_" } } else { $output | ForEach-Object { "$_" } }
exit $code
