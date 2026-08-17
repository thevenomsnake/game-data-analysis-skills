# Setup Onboarding

The public edition uses a local-only, status-first setup flow.

1. Check the selected folder with `setup/scripts/bootstrap_repo.py status`.
2. Clone or fast-forward only an empty folder or a clean checkout with `sync`.
3. Install `sql-engineering/` into the local Codex skills directory.
4. Create the fictional `example` project with `bootstrap_repo.py demo`.
5. Configure a local read-only adapter only when the user has one; otherwise use manual handoff.

No step requires a company account, LDAP identity, private Git host, browser session, or database
password. Project-specific source files and rules are supplied later by the user and remain local.

The setup scripts refuse to overwrite dirty worktrees and preserve existing project files. Their
outputs are JSON so a caller can inspect status without guessing from console text.
