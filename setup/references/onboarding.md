# Public Onboarding

The public edition is self-contained. It does not require GitLab, LDAP, SVN, a DA console, or a
company database.

## Recommended path

```powershell
python .\setup\scripts\bootstrap_repo.py status --root .
python .\setup\scripts\bootstrap_repo.py sync --root .
python .\setup\scripts\bootstrap_repo.py demo --root .
Copy-Item -Recurse .\setup "$HOME\.codex\skills\setup"
Copy-Item -Recurse .\sql-engineering "$HOME\.codex\skills\sql-engineering"
```

Run `sync` only in an empty folder or a clean checkout. Run `demo` whenever you want a fictional
project scaffold; it preserves an existing project.

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
