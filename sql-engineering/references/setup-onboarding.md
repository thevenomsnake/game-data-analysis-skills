# Setup Onboarding

The public edition uses a local-only, status-first setup flow.

1. Check the selected folder with `setup/scripts/bootstrap_repo.py status`.
2. Clone or fast-forward only an empty folder or a clean checkout with `sync --remote <git-url>`.
3. Run `configure` to record the Git host/remote/branch and choose `git`, `svn`, `local`, or
   `none` for planning sources.
4. Install `setup/` and `sql-engineering/` into the local Codex skills directory.
5. Run `planning-sync` when Git or SVN owns the planning source.
6. Create the fictional `example` project with `bootstrap_repo.py demo`.
7. Select an execution surface: configure a direct DB-API/CLI profile, copy and validate a local
   web adapter, or keep the project on manual handoff.

Git is required, but the hosting service is not fixed. Credentials remain in the user's Git/SVN
credential mechanism. Project-specific source files and rules stay local and ignored.

The setup scripts refuse to overwrite dirty worktrees and preserve existing project files. Their
outputs are JSON so a caller can inspect status without guessing from console text.
