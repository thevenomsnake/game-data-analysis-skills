# Public Maintenance

The public repository is a derived, deidentified release surface. The maintained source workspace
is authoritative when behavior conflicts; this repository receives reviewed generic capabilities,
schemas, documentation, examples, and tests from that source.

The export boundary is strict:

- exclude the personal `BetterXml` tool;
- exclude project directories, production SQL, result evidence, workbooks, credentials, endpoints,
  and local workspaces;
- retain generic code and contracts after replacing project-specific facts with fictional examples;
- run `tools/public_release.py validate` and the focused test suite before a release;
- publish only the generated public tree, never the internal source history.

The `release-manifest.json` command output is a local audit artifact and is intentionally ignored by
Git. A public release should have a clean commit containing only the validated tree.

Installation is transport-neutral: Git is the baseline dependency, while the remote host is a
configuration value. Planning sources are independent and may use Git, SVN, a local folder, or
`none`; provider metadata belongs in ignored `.local/setup-config.json`, never in the public source.

## Source Sync Allowlist

The maintained source and this repository are separate trees. Do not copy the source tree wholesale.
`tools/public-sync-allowlist.json` records the reviewed capability roots, explicit exclusions, public-
only adaptations, and exact files whose normalized content must match the source.

Run the audit from the public checkout and point it at the maintained source:

```powershell
python .\tools\public_sync.py audit `
  --source-root <maintained-source-root> `
  --public-root . `
  --format json
```

`source_only`, `exact_missing`, `exact_drift`, or `forbidden` blocks a release. `changed_review` is
reported for intentionally deidentified public adaptations and must be reviewed against the source
commit before publishing. Update the allowlist only after deciding whether a new source path is a
generic capability, a public-only adaptation, or an excluded project/production surface. The audit
never copies files and never writes the source tree.
