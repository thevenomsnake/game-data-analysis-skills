# Security Policy

## Scope

Security reports may cover unsafe path handling, workspace corruption, credential exposure, or
release content that should not be public.

## Reporting

Do not open a public issue containing credentials or private data. Use GitHub's private
vulnerability reporting feature for this repository.

## Data Boundary

This project must not contain production SQL results, credentials, private endpoints,
organization-specific table definitions, or local absolute paths. The public Skill stores SQL
inside the project selected by the user and never uploads it on its own.

Database execution is optional and read-only. Keep connection options in the ignored
`.sql-engineering/connections.local.json` file or another local path. Supply passwords and tokens
through environment variables only. The public Skill does not automate browser or DA-console
sessions, and local execution results under `.sql-engineering/runs/` are not committed by default.
Connection profiles select Python modules or executable database clients and must therefore come
from a trusted local source, never from an unreviewed repository or message attachment.
