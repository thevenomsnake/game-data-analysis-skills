[CmdletBinding()]
param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [int]$LogCount = 5
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string[]]$Args
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Git can write advisory warnings to stderr while still exiting 0.
        # Capture those lines without letting Windows PowerShell promote them to terminating errors.
        $ErrorActionPreference = "Continue"
        $output = & git -C $Repo @Args 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    [pscustomobject]@{
        Code  = $code
        Lines = @($output | ForEach-Object { "$_" })
    }
}

function Get-RelativeRepoPath {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Path
    )

    if ($Base -eq $Path) {
        return "."
    }

    return [System.IO.Path]::GetRelativePath($Base, $Path)
}

function Find-GitRepos {
    param([Parameter(Mandatory = $true)][string]$RootPath)

    $repos = New-Object "System.Collections.Generic.List[string]"
    $rootFull = (Resolve-Path $RootPath).Path

    if (Test-Path -LiteralPath (Join-Path $rootFull ".git")) {
        $repos.Add($rootFull)
    }

    function Walk {
        param([Parameter(Mandatory = $true)][string]$Dir)

        foreach ($child in Get-ChildItem -LiteralPath $Dir -Force -Directory) {
            if ($child.Name -eq ".git") {
                continue
            }
            if ($child.Name -eq "__pycache__") {
                continue
            }
            if ($child.Name -like ".tmp-*") {
                continue
            }

            $gitMarker = Join-Path $child.FullName ".git"
            if (Test-Path -LiteralPath $gitMarker) {
                $repos.Add($child.FullName)
            }

            Walk -Dir $child.FullName
        }
    }

    Walk -Dir $rootFull

    return $repos | Select-Object -Unique
}

function Get-RootTrackingKind {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$RepoPath
    )

    if ($RootPath -eq $RepoPath) {
        return "root"
    }

    $relative = (Get-RelativeRepoPath -Base $RootPath -Path $RepoPath).Replace("\", "/")
    $tracked = Invoke-Git -Repo $RootPath -Args @("ls-files", "--stage", "--", $relative)

    if ($tracked.Code -ne 0 -or $tracked.Lines.Count -eq 0) {
        return "standalone-nested-repo"
    }

    $gitlink = $tracked.Lines | Where-Object { $_ -match "^160000\s" }
    if ($gitlink) {
        return "submodule/gitlink"
    }

    return "parent-tracked-files"
}

function Write-GitBlock {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)]$Result,
        [string]$EmptyText = "(none)"
    )

    Write-Output ""
    Write-Output "${Title}:"

    if ($Result.Code -ne 0) {
        if ($Result.Lines.Count -gt 0) {
            $Result.Lines | ForEach-Object { Write-Output "  $_" }
        } else {
            Write-Output "  (command failed)"
        }
        return
    }

    if ($Result.Lines.Count -eq 0) {
        Write-Output "  $EmptyText"
        return
    }

    $Result.Lines | ForEach-Object { Write-Output "  $_" }
}

$rootFullPath = (Resolve-Path $Root).Path
$repos = @(Find-GitRepos -RootPath $rootFullPath)

Write-Output "# Git Sync Audit"
Write-Output "Root: $rootFullPath"
Write-Output "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
Write-Output "Repositories found: $($repos.Count)"

foreach ($repo in $repos) {
    $repoFullPath = (Resolve-Path $repo).Path
    $relativePath = Get-RelativeRepoPath -Base $rootFullPath -Path $repoFullPath
    $trackingKind = Get-RootTrackingKind -RootPath $rootFullPath -RepoPath $repoFullPath

    Write-Output ""
    Write-Output "## $relativePath"
    Write-Output "Path: $repoFullPath"
    Write-Output "Root tracking: $trackingKind"

    $branch = Invoke-Git -Repo $repoFullPath -Args @("branch", "--show-current")
    if ($branch.Code -eq 0 -and $branch.Lines.Count -gt 0 -and $branch.Lines[0]) {
        Write-Output "Branch: $($branch.Lines[0])"
    } else {
        Write-Output "Branch: (detached or unknown)"
    }

    $upstream = Invoke-Git -Repo $repoFullPath -Args @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if ($upstream.Code -eq 0 -and $upstream.Lines.Count -gt 0 -and $upstream.Lines[0]) {
        Write-Output "Upstream: $($upstream.Lines[0])"
    } else {
        Write-Output "Upstream: (none)"
    }

    Write-GitBlock -Title "Remotes" -Result (Invoke-Git -Repo $repoFullPath -Args @("remote", "-v"))
    Write-GitBlock -Title "Status" -Result (Invoke-Git -Repo $repoFullPath -Args @("status", "--short", "--branch"))
    Write-GitBlock -Title "Staged diff stat" -Result (Invoke-Git -Repo $repoFullPath -Args @("diff", "--cached", "--stat"))
    Write-GitBlock -Title "Unstaged diff stat" -Result (Invoke-Git -Repo $repoFullPath -Args @("diff", "--stat"))
    Write-GitBlock -Title "Untracked files" -Result (Invoke-Git -Repo $repoFullPath -Args @("ls-files", "--others", "--exclude-standard"))
    Write-GitBlock -Title "Recent commits" -Result (Invoke-Git -Repo $repoFullPath -Args @("log", "--oneline", "--decorate", "--max-count=$LogCount"))
}
