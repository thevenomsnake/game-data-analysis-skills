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
