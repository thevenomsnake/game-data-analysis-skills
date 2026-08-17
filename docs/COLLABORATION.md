# Collaboration

The public edition keeps collaboration local and explicit. Run:

```powershell
python .\sql-engineering\scripts\collaboration_submit.py plan --repo-root .
```

The command lists staged, modified, untracked, and blocked paths. It excludes project data,
results, credentials, local workspaces, and the personal `BetterXml` tool. `submit` returns a
`manual_required` receipt; use the user's normal Git client for the final commit or push.

There is no bundled remote API client and no automatic credential handling.
