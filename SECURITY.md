# Security

Do not commit credentials, tokens, private endpoints, production SQL, result files, private table
definitions, or personal workspace paths. Keep connection profiles in the ignored
`.sql-engineering/connections.local.json` file and secrets in local environment variables.

The public setup flow does not request or store a password. Automatic execution is optional and
must use a configured read-only DB-API/CLI adapter or an explicitly selected Chrome plugin route
with the user's own authenticated session. No browser endpoint or credential is bundled.

Git and SVN remotes are configuration, not credential stores. Never embed a username/password or
token in an HTTP URL; use the user's native Git/SVN credential mechanism. Local setup metadata and
provider checkouts remain under ignored `.local/`.

Report suspected disclosure privately to the repository maintainer. If a private value was
published, rotate it first and preserve the affected commit hash for cleanup.
