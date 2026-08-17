[CmdletBinding()]
param(
    [string]$SourcePath = (Join-Path $PSScriptRoot "..\sql-engineering"),
    [string]$TargetRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if (-not $TargetRoot) {
    $TargetRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "skills" } else { Join-Path $HOME ".codex\skills" }
}
$source = (Resolve-Path -LiteralPath $SourcePath).Path
$targetRootFull = [IO.Path]::GetFullPath($TargetRoot)
$target = Join-Path $targetRootFull "sql-engineering"

$rootPrefix = $targetRootFull.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to operate outside target root: $target"
}

if (-not (Test-Path -LiteralPath (Join-Path $source "SKILL.md"))) { throw "SKILL.md is missing: $source" }
Write-Output "source: $source"
Write-Output "target: $target"
if ($DryRun) { return }

New-Item -ItemType Directory -Path $targetRootFull -Force | Out-Null
& (Join-Path $PSScriptRoot "verify-sql-engineering.ps1") -Root (Join-Path $PSScriptRoot "..")
if ($LASTEXITCODE -ne 0) { throw "Verification failed; deployment stopped." }

$backup = $null
if (Test-Path -LiteralPath $target) {
    $backupRoot = Join-Path $targetRootFull ".skill-backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backup = Join-Path $backupRoot (Get-Date -Format "yyyyMMddTHHmmss")
    Move-Item -LiteralPath $target -Destination $backup
}
Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
Write-Output "deployed: $target"
if ($backup) { Write-Output "backup: $backup" }
