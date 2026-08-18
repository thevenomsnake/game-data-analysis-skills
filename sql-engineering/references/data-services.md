# Data Services

Data services are explicit, purpose-specific adapters declared by a project. A service may be a
read-only development inspection connection, a local DB-API/CLI execution profile, or an optional
browser adapter selected by the user. Sharing a host or adapter does not transfer business rules
or source bindings between projects.

Keep three facts separate:

1. The project configuration names the purpose and dialect.
2. The ignored local connection file supplies the trusted adapter details.
3. Result receipts record the exact SQL hash, environment, adapter, columns, and row count.

Secrets never belong in Git, SQL, receipts, URLs, or chat. Missing configuration returns a clear
`manual_required` or `credential_required` state. Do not silently switch environments or claim
that a development observation is production evidence.

For browser execution, use only the Chrome plugin with the user's own session. The public package
contains no cookie, token, or credential automation. Resolve the project's ignored
`web_query_adapter_v1` before opening a query page; see `execution-surfaces.md`. Direct DB-API/CLI,
web, and manual handoff are separate execution surfaces and must not silently fall back to one
another.
