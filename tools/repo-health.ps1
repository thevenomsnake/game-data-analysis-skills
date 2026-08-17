[CmdletBinding()]
param(
    [string]$Root = (Join-Path $PSScriptRoot ".."),
    [ValidateSet("text", "json")][string]$Format = "text"
)

$ErrorActionPreference = "Stop"
$rootFull = (Resolve-Path -LiteralPath $Root).Path
$python = (Get-Command python -ErrorAction Stop).Source
$checks = @()

$release = & $python (Join-Path $rootFull "tools\public_release.py") validate --root $rootFull 2>&1
$checks += [pscustomobject]@{ id = "public_release"; status = if ($LASTEXITCODE -eq 0) { "pass" } else { "fail" }; output = ($release -join "`n") }

$compile = & $python -m compileall -q (Join-Path $rootFull "sql-engineering\scripts") (Join-Path $rootFull "setup\scripts") 2>&1
$checks += [pscustomobject]@{ id = "python_compile"; status = if ($LASTEXITCODE -eq 0) { "pass" } else { "fail" }; output = ($compile -join "`n") }

$status = if ($checks.status -contains "fail") { "fail" } else { "pass" }
$payload = [pscustomobject]@{ schema_version = "public_repo_health_v1"; status = $status; root = $rootFull; checks = $checks }
if ($Format -eq "json") { $payload | ConvertTo-Json -Depth 10 } else { $payload | Format-List }
if ($status -eq "fail") { exit 1 }
