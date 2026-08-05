# SQL Lifecycle

The public edition keeps the lifecycle deliberately small:

```text
request
  -> saved temporary query version
  -> configured read-only database execution
     or exact SQL handoff for manual execution
  -> optional result evidence
  -> human decision about reuse
     -> keep as workspace history
     -> save a retained query version
     -> derive a dashboard in a project-specific workflow
```

## Query Families

A query family represents one analytical question. Keep revisions together when they correct
syntax, change parameters, or add output that fully contains the earlier question. Start a new
family when the population, primary metric, or decision changes.

Examples:

- Changing a date window: same family.
- Adding a requested dimension to the same metric: usually the same family.
- Replacing an incorrect predicate with the confirmed one: same family, new version.
- Moving from active-user counts to retention: new family.

## Lifecycle States

- `temporary`: runnable analysis that may still change.
- `retained`: a reusable query whose logic is worth keeping.
- `dashboard`: SQL derived for a presentation or refresh contract.

These labels describe intended use. They do not prove correctness or execution.

## Result Evidence

Keep execution evidence separate from SQL. Record the exact SQL version and content hash that
produced a result. Never bind a result to a title alone, and never overwrite historical SQL to
make a result appear current.

## Execution Decision

After saving and verifying SQL, run `sql_execute.py` only when the project has a configured
database environment. A `ready` execution receipt is direct evidence. A `manual_required`
receipt means the SQL remains ready but the user must run it and return the result. Do not use a
browser or a web-based DA console as an automatic fallback.
