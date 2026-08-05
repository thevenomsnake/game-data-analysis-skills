---
name: sql-engineering
description: Use this skill for durable SQL work where every generated or modified query must be saved, versioned, indexed, and delivered by exact file path. It supports project initialization, immutable temporary and retained SQL versions, external SQL intake, searchable summaries, and verified delivery receipts without embedding organization-specific schemas or business rules.
metadata:
  short-description: File-backed, versioned SQL delivery
  version: "1.0.2"
---

# SQL Engineering

## Goal

Turn SQL conversations into durable, searchable files. A SQL answer is incomplete until the
exact runnable version is saved and a ready delivery receipt confirms its absolute path and
content hash.

## Start Here

Read `references/workflow.md` for lifecycle decisions. Read
`references/project-contract.md` when initializing or repairing a workspace. Read
`references/sql-quality.md` before delivering executable SQL.

Read `references/example.md` when onboarding a new project or when the expected saved files,
index entry, and final delivery response are unclear. The bundled example SQL is executable
input for the storage workflow and uses fictional source names that must be replaced before
database execution.

Use `scripts/sql_workspace.py` for deterministic storage and retrieval:

```powershell
python <skill-root>/scripts/sql_workspace.py init --root <project-root> --project-id <id> --dialect <dialect>
python <skill-root>/scripts/sql_workspace.py save --root <project-root> --sql-file <input.sql> --title <title> --summary <summary>
python <skill-root>/scripts/sql_workspace.py receipt --root <project-root> --sql-file <saved-vNNN.sql>
python <skill-root>/scripts/sql_workspace.py search --root <project-root> --query <text>
```

## Hard Boundaries

1. Never use a chat code block as the only SQL deliverable.
2. Never edit an external SQL file in place. Save a project-local immutable version.
3. Every generated or modified SQL must have a concise title and summary in the index.
4. Save a new `vNNN.sql` when executable SQL changes. Never overwrite an existing version.
5. A request that expands an existing analytical question stays in the same query family. A
   materially different business question gets a new family.
6. A ready receipt must match both the saved metadata hash and the current file hash.
7. Select the dialect from project configuration. Do not infer a database, table, partition
   field, business ID, or date policy from this public Skill.
8. Put reusable date and scope values in a short `params` CTE when the target dialect supports
   it. Keep the SQL directly runnable with concrete values.
9. Apply only business rules supplied by the user or the current project. This Skill ships no
   organization-specific metric definitions.
10. Do not claim that SQL ran successfully without execution evidence supplied by the user or
    observed from an execution tool.
11. SQL-side privacy transformations are not invented automatically. Follow the user's data
    platform policy and preserve business semantics.
12. Generated indexes and metadata may describe lifecycle state, but they do not approve,
    publish, or promote an asset by themselves.

## Workflow

### Generate Or Modify SQL

1. Identify the project root and read `.sql-engineering/project.json`.
2. Search the workspace before creating a duplicate query family.
3. Draft or modify SQL in a temporary input file.
4. Run the quality checks in `references/sql-quality.md`.
5. Save with `sql_workspace.py save` and a human-readable summary.
6. Run `sql_workspace.py receipt` on the saved version.
7. Return the receipt's absolute `delivery_file` path.

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
