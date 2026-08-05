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
