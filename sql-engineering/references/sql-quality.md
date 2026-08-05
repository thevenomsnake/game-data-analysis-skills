# SQL Quality Gate

Before saving a runnable version, check:

- The configured dialect and execution environment are explicit.
- Event and field semantics come from a registered original telemetry source version.
- Applied business rules cite registered source or knowledge versions and are current confirmed definitions.
- Planning inputs are not presented as human-confirmed knowledge or canonical rules.
- Every large source has a bounded time or partition predicate.
- Date bounds use the project's documented inclusive/exclusive convention.
- Required scope filters use concrete values or named parameters.
- The final output grain is clear.
- Distinct-count and ratio denominators cannot be multiplied by joins.
- Detail queries have a deliberate row limit when appropriate.
- Production SQL avoids `SELECT *`.
- Identifiers and string literals are quoted for the target dialect.
- No execution, verification, or business-rule claim lacks evidence.
- Automatic execution uses one saved, receipt-verified, read-only statement.
- The saved SQL dialect matches the selected database environment.
- Missing automatic connection configuration produces a manual handoff, not browser automation.

Prefer a short parameter CTE for reusable values:

```sql
WITH params AS (
    SELECT
        CAST('2026-01-01' AS DATE) AS start_date,
        CAST('2026-01-31' AS DATE) AS end_date,
        42 AS zone_id
)
```

The exact casts and date predicates remain dialect-specific. Never copy this example without
checking the project's execution contract.
