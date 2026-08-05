---
name: sql-engineering
description: Use this skill for durable SQL projects that must register raw telemetry definitions, separate planning and human-confirmed knowledge, fix versioned canonical rules, select SQL dialect and database environment, and save every generated or modified query as an indexed immutable file. It supports configurable read-only database execution and exact manual SQL handoff without organization-specific schemas or rules.
metadata:
  short-description: Governed project context and versioned SQL execution
  version: "1.3.0"
---

# SQL Engineering

## Goal

Turn SQL conversations into durable, searchable files. A SQL answer is incomplete until the
exact runnable version is saved and a ready delivery receipt confirms its absolute path and
content hash.

## Start Here

Read `references/workflow.md` for lifecycle decisions. Read
`references/project-contract.md` when initializing or repairing a workspace. Read
`references/sql-quality.md` before delivering executable SQL. Read
`references/database-execution.md` before configuring or using automatic execution.
Read `references/dialects.md` before selecting SQL syntax or mapping a connection method to an engine.
Read `references/project-onboarding.md` before creating a project or when source, planning,
confirmed knowledge, rule ownership, dialect, or connection setup is unclear.

Read `references/example.md` when onboarding a new project or when the expected saved files,
index entry, and final delivery response are unclear. The bundled example SQL is executable
input for the storage workflow and uses fictional source names that must be replaced before
database execution.

Use `scripts/sql_workspace.py` for deterministic storage and retrieval:

```powershell
python <skill-root>/scripts/sql_workspace.py bootstrap --root <workspace-root> --project-id <id> --dialect <dialect>
python <skill-root>/scripts/sql_workspace.py init --root <project-root> --project-id <id> --dialect <dialect>
python <skill-root>/scripts/sql_workspace.py environment --root <project-root> --name <environment> --dialect <dialect> --connection-profile <profile> --default
python <skill-root>/scripts/sql_workspace.py source --root <project-root> --file <definition-file> --name <name> --description <description>
python <skill-root>/scripts/sql_workspace.py knowledge --root <project-root> --file <file> --kind <planning|confirmed> --name <name> --description <description>
python <skill-root>/scripts/sql_workspace.py rule --root <project-root> --rule-file <rule.json> --confirmed-by <person> --confirmation-note <reason>
python <skill-root>/scripts/sql_workspace.py status --root <project-root>
python <skill-root>/scripts/sql_workspace.py save --root <project-root> --sql-file <input.sql> --title <title> --summary <summary>
python <skill-root>/scripts/sql_workspace.py receipt --root <project-root> --sql-file <saved-vNNN.sql>
python <skill-root>/scripts/sql_workspace.py search --root <project-root> --query <text>
python <skill-root>/scripts/sql_execute.py run --root <project-root> --sql-file <saved-vNNN.sql>
```

## Hard Boundaries

1. On first use of a repository, run `bootstrap` when `sql-projects/` or its reserved cross-project directories are missing. Store normal projects under `sql-projects/<project-id>`.
2. Never use a chat code block as the only SQL deliverable.
3. Never edit an external SQL file in place. Save a project-local immutable version.
4. Every generated or modified SQL must have a concise title and summary in the index.
5. Save a new `vNNN.sql` when executable SQL changes. Never overwrite an existing version.
6. A request that expands an existing analytical question stays in the same query family. A
   materially different business question gets a new family.
7. A ready receipt must match both the saved metadata hash and the current file hash.
8. Select the dialect and execution environment from project configuration. Do not infer a
   database, table, partition field, business ID, or date policy from this public Skill.
9. Put reusable date and scope values in a short `params` CTE when the target dialect supports
   it. Keep the SQL directly runnable with concrete values.
10. Apply only business rules supplied by the user or the current project. This Skill ships no
   organization-specific metric definitions.
11. Do not claim that SQL ran successfully without a ready `sql_execution_receipt_v1` or
    execution evidence returned by the user.
12. SQL-side privacy transformations are not invented automatically. Follow the user's data
    platform policy and preserve business semantics.
13. Generated indexes and metadata may describe lifecycle state, but they do not approve,
    publish, or promote an asset by themselves.
14. Use only project-declared context files. Never depend on a personal knowledge-base path. If
    context is absent, inspect metadata or enums through a saved read-only database query.
15. Automatic execution supports only project-configured DB-API drivers or database command-line
    clients. Never automate a web page, Chrome, or a DA console in this public Skill.
16. Keep hosts, users, and connection options in the local ignored connection file; obtain
    passwords and tokens only from environment variables. Never commit credentials.
17. Automatic execution is read-only and accepts one saved query version. If no automatic
    connection is configured or available, return `manual_required`, deliver the SQL path, and
    ask the user to run it and return the result file. This is a normal workflow, not a failure.
18. Before authoritative project SQL, register at least one original telemetry definition. Preserve
    XML, JSON, YAML, Excel, CSV, text, or another supplied format unchanged and cite its versioned ID.
19. Store original planning/configuration tables under `knowledge/planning`; they are evidence, not
    confirmed truth. Store manually reviewed material under `knowledge/confirmed` with confirmer and reason.
20. Fix business logic only through an explicit `rule` request. A canonical rule must cite exact source
    or knowledge IDs, creates an immutable `vNNN.json`, and is never changed by ordinary SQL work.
21. Use `context/` only for non-authoritative notes and platform manuals. Do not treat a file as a rule
    merely because it appears in project context or a chat message.

## Workflow

### Create A Project

1. Run `bootstrap` with the project ID and generation dialect.
2. Register original telemetry files with `source`.
3. Register planning tables and separately register human-confirmed knowledge.
4. Declare database environments and local connection profiles when automatic execution is available.
5. Fix only explicitly confirmed canonical rules with `rule`.
6. Run `status`; resolve source blockers before authoritative SQL generation.

### Generate Or Modify SQL

1. Identify the project root, run `status`, and read `.sql-engineering/project.json`.
2. Read relevant registered source, knowledge, and current rule versions.
3. Search the workspace before creating a duplicate query family.
4. Draft or modify SQL in a temporary input file using the selected environment dialect.
5. Run the quality checks in `references/sql-quality.md`.
6. Save with `sql_workspace.py save` and a human-readable summary.
7. Run `sql_workspace.py receipt` and return its absolute `delivery_file` path.

### Execute Or Hand Off

1. Use the environment saved with the SQL, an explicitly requested environment, or the project default.
2. Run `sql_execute.py run` only against a ready saved SQL receipt.
3. On `ready`, return the exact result and execution-receipt paths.
4. On `manual_required`, return the SQL path and ask the user to run it and send back the result.
5. On `blocked` or `failed`, report the recorded error without claiming successful execution.

### External SQL

Treat the supplied file as immutable evidence. Read it, create a revised working file, and save
that revision through the workspace command. The stored metadata records the source filename
and source hash, not a machine-specific absolute source path.

### Revisions

Use the same slug when the new SQL corrects, parameterizes, or fully extends the same question.
The new version supersedes the earlier runnable text while history remains available. Use a new
slug when the Base, primary metric family, or analytical decision changes.

### Results And Dashboards

Result files, visualizations, validation, and dashboards are separate lifecycle objects. Do not
silently treat a returned result as proof that a query should be retained or promoted. Projects
may add their own governed extensions for these stages.

## Delivery

The final response names what changed, states what was not executed, and links the exact saved
SQL file. If the receipt is blocked, fix the storage or content mismatch before presenting SQL
as ready.
