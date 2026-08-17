# Public Onboarding

The public edition is self-contained. Git is the baseline dependency; GitHub, GitLab, self-hosted
Git, and local Git are transport choices. SVN is optional when the planning source uses it. No
company database or DA console is required for setup.

## Recommended path

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py sync --root .
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --remote https://github.com/thevenomsnake/game-data-analysis-skills.git `
  --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Run `sync` only in an empty folder or a clean checkout. Run `demo` whenever you want a fictional
project scaffold; it preserves an existing project.

The repository remote is always Git, while the hosting service is inferred from the URL and can be
GitHub, GitLab, a self-hosted service, SSH, or a local repository. Configure the planning source
separately:

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git --planning-url <git-url> --planning-branch main
python .\setup\scripts\bootstrap_repo.py planning-sync --root .
```

Replace `git` with `svn`, `local`, or `none` as appropriate. The configuration is stored under
ignored `.local/`; only non-secret provider, URL, branch, and revision metadata is written.
Passing `--planning-path` selects a user-managed checkout and setup does not mutate it. Passing
`--planning-url` lets setup maintain its own checkout under `.local/planning-sources/`.
`configure` also writes `/.local/` to `.git/info/exclude`; this is local Git metadata and does not
change the downstream repository's tracked `.gitignore`.

## First SQL exercise

```powershell
python .\sql-engineering\scripts\sql_workspace.py bootstrap `
  --root . `
  --project-id example `
  --dialect sqlite

python .\sql-engineering\scripts\sql_workspace.py save `
  --root .\sql-projects\example `
  --sql-file .\sql-engineering\assets\examples\daily-active-users.sql `
  --title "Daily active users" `
  --summary "Counts distinct fictional users by activity date." `
  --kind temporary `
  --slug daily-active-users
```

The command returns an exact versioned path. Run `receipt` before handing the file to another
person or tool. Automatic execution is optional and must use a configured read-only adapter.

## Optional advanced flows

The public `sql-engineering` directory also contains the governed query workspace, rule store,
formal asset package, review, result-lineage, planning-source, and visualization modules. They
are all file-backed and can be used with a project created by `sql_project.py init`.
