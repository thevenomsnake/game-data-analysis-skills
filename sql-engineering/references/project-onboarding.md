# Project Onboarding

A new project is usable only after its local data context is explicit. The public Skill ships a
workflow and file contracts; it does not ship anyone's telemetry, planning tables, business rules,
database endpoints, or credentials.

## What To Prepare

| Input | Why it is needed | Project location |
|---|---|---|
| Raw telemetry definition | Explains event and field meaning; XML, JSON, YAML, Excel, CSV, text, or another original format is accepted | `sources/raw/` |
| Planning and configuration tables | Preserve design-owned mappings, IDs, thresholds, and configuration evidence | `knowledge/planning/` |
| Human-confirmed material | Records the exact reference that a named person reviewed and accepted | `knowledge/confirmed/` |
| Canonical rules | Fixes the business definition, Base, grain, formula, filters, and cited evidence used by SQL | `rules/definitions/` |
| Database environment | Declares the environment name, SQL dialect, and local connection-profile name | `.sql-engineering/project.json` |
| Local connection details | Supplies a trusted DB-API driver or database CLI without committing credentials | `.sql-engineering/connections.local.json` |

`context/` is for non-authoritative project notes and platform documentation. Do not put a rule there
and assume it became canonical.

## 1. Create The Project

```powershell
python <skill-root>/scripts/sql_workspace.py bootstrap `
  --root <workspace-root> `
  --project-id example `
  --dialect starrocks
```

This creates the project, SQL workspace, source catalog, knowledge catalog, rule catalog, and the
corresponding directories. Running the same command again repairs missing empty structure and preserves
registered content.

## 2. Register The Raw Telemetry Definition

Keep the original file unchanged. Registration copies its exact bytes, records its hash, and returns a
versioned source ID. The format is not restricted to XML.

```powershell
python <skill-root>/scripts/sql_workspace.py source `
  --root <project-root> `
  --file <path-to-original-telemetry-file> `
  --name "PlayerLogin telemetry" `
  --description "Raw event and field definition supplied by the telemetry owner." `
  --slug player-login
```

The returned ID, such as `player-login:v001`, is the stable reference used by rules. Re-registering the
same file does not create a duplicate. A changed definition becomes `v002`; it does not overwrite `v001`.

## 3. Register Planning And Confirmed Knowledge

A planning table is source evidence, not an automatically trusted rule:

```powershell
python <skill-root>/scripts/sql_workspace.py knowledge `
  --root <project-root> `
  --file <path-to-planning-table.xlsx> `
  --kind planning `
  --name "Game mode planning table" `
  --description "Original mode IDs and names supplied by design." `
  --slug game-mode-table
```

When a person reviews and confirms a structured reference, register that exact confirmed file separately.
Use `--based-on` to preserve its relationship to the planning input:

```powershell
python <skill-root>/scripts/sql_workspace.py knowledge `
  --root <project-root> `
  --file <path-to-confirmed-mapping.json> `
  --kind confirmed `
  --name "Confirmed game mode mapping" `
  --description "Mode mapping approved for analytical use." `
  --slug game-mode-mapping `
  --based-on planning:game-mode-table:v001 `
  --confirmed-by "analyst@example" `
  --confirmation-note "Reviewed against the current design table."
```

Manual confirmation records who confirmed what and why. It does not silently create or update a
canonical rule.

## 4. Declare SQL Dialect And Database Access

The project dialect controls generated SQL syntax. Each named environment maps that dialect to a trusted
local connection profile:

```powershell
python <skill-root>/scripts/sql_workspace.py environment `
  --root <project-root> `
  --name development `
  --dialect starrocks `
  --connection-profile development-starrocks `
  --default
```

Then choose one execution surface in `execution-surfaces.md`. Configure a local DB-API/database CLI
profile for direct execution, or an ignored `web_query_adapter_v1` for a user-selected browser query
product. If neither is configured, SQL generation and storage still work; the Skill returns the exact
SQL file for manual execution and asks the user to return the result.

Connection protocol and SQL syntax are separate. For example, StarRocks may use a MySQL-compatible driver
while the project dialect remains `starrocks`. Read `dialects.md` for the selection matrix and rules.

## 5. Fix A Canonical Rule

Start from `assets/examples/canonical-rule.example.json`. A rule must state a readable business definition,
grain, calculation, filters, and the exact source or knowledge IDs it relies on.

```powershell
python <skill-root>/scripts/sql_workspace.py rule `
  --root <project-root> `
  --rule-file <canonical-rule-input.json> `
  --confirmed-by "analyst@example" `
  --confirmation-note "Approved for this project's current analytical use."
```

This creates `rules/definitions/<concept-key>/vNNN.json` and updates only the rule catalog's current pointer.
Historical definitions remain immutable. Ordinary SQL generation, modification, review, or execution must
never invoke this command unless the user explicitly asks to manage or confirm a rule.

## 6. Check Project Readiness

```powershell
python <skill-root>/scripts/sql_workspace.py status --root <project-root>
```

The status reports raw source, planning knowledge, confirmed knowledge, canonical rule, and execution
environment counts. `query_context_ready=false` means no raw telemetry definition is registered. Missing
automatic execution is only a warning because manual SQL handoff remains available.

## 7. Generate, Save, And Execute SQL

The agent now has explicit evidence ownership:

1. Read the registered raw telemetry source for event and field meaning.
2. Read only relevant planning and confirmed knowledge versions.
3. Apply current canonical rules that cite those versions.
4. Generate SQL in the selected environment's dialect.
5. Save and receipt the immutable SQL version.
6. Execute through the selected direct/web surface, or hand the exact SQL file to the user.
7. Bind any returned result to that exact SQL version and content hash.

This ordering prevents a planning table from becoming a rule by accident, a chat correction from silently
changing canonical logic, or a query from depending on an undeclared personal knowledge base.
