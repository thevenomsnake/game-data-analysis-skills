# Security

Do not commit credentials, tokens, private endpoints, production SQL, result files, private table
definitions, or personal workspace paths. Keep connection profiles in the ignored
`.sql-engineering/connections.local.json` file and secrets in local environment variables.

The public setup flow does not request or store a password. Automatic execution is optional and
must use a configured read-only DB-API driver or command-line client. Browser and DA-console
automation is outside this public package.

Report suspected disclosure privately to the repository maintainer. If a private value was
published, rotate it first and preserve the affected commit hash for cleanup.
