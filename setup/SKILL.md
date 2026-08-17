---
name: setup
description: Initialize the public Game Data Analysis Skills workspace locally.
metadata:
  short-description: Local setup for the public SQL Engineering Skill
  version: "1.0.0"
---

# Setup

Use this skill when a user downloads the public repository and wants a working local
workspace. It never hard-codes a hosting service, never stores a credential, and contacts only
the Git/SVN/local source explicitly configured by the user.

## First run

1. Choose an empty folder for the repository.
2. Run `setup/scripts/bootstrap_repo.py sync --root <folder> --remote <git-url>` or clone the
   configured Git repository. The public GitHub URL is only the default.
3. Run `setup/scripts/bootstrap_repo.py configure` to record the Git remote/provider and the
   planning-source provider.
4. Install `setup/` and `sql-engineering/` into the local Codex skills directory.
5. Run `setup/scripts/bootstrap_repo.py demo --root <folder>` to create a fictional project.
6. Use the generated project with `$sql-engineering`.

The setup command is idempotent. It refuses to overwrite a non-empty non-repository folder and
does not modify a dirty checkout. Database connections are optional; manual SQL handoff is enough
for the first run.

Git is the only required source-control dependency. GitHub, GitLab, self-hosted Git, SSH remotes,
and local Git repositories are all selected by URL and are not hard-coded into the workflow.
Planning sources are configured independently as `git`, `svn`, `local`, or `none`.

## Local commands

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --remote https://github.com/thevenomsnake/game-data-analysis-skills.git `
  --planning-provider none
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
```

The `demo` command creates only configuration, empty catalogs, and a fictional query example.
It does not create results or connect to a database.

For a Git-managed planning source:

```powershell
python .\setup\scripts\bootstrap_repo.py configure --root . `
  --planning-provider git `
  --planning-url <git-planning-repository> `
  --planning-branch main `
  --planning-id planning
python .\setup\scripts\bootstrap_repo.py planning-sync --root .
```

Use `--planning-provider svn --planning-url <svn-url>` for SVN, or
`--planning-provider local --planning-path <folder>` for a user-managed folder. Credentials are
handled by Git/SVN's local credential mechanism; setup never writes them to the config file.
For Git or SVN, `--planning-path` means an existing user-managed checkout and is never updated by
setup; `--planning-url` means setup owns an ignored local checkout and may fast-forward/update it.
Setup also adds `/.local/` to the repository-local `.git/info/exclude`, so the configuration stays
untracked even when a downstream repository has a different `.gitignore`.

## Safety

The public workspace must not contain production SQL, result files, credentials, private table
definitions, or machine-specific absolute paths. Keep local connection profiles under the ignored
`.sql-engineering/` directory and provide secrets through the local environment only.
