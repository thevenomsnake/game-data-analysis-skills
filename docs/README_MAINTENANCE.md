# README Maintenance

This is the release contract for the six public README files. README changes are product changes:
they define installation expectations, capability boundaries, supported interfaces, and the first
path a new user follows.

## Maintained Files

| Locale | File | Stable language name |
|---|---|---|
| `en` | `README.md` | `English` |
| `zh-CN` | `README.zh-CN.md` | `简体中文` |
| `zh-TW` | `README.zh-TW.md` | `繁體中文` |
| `ja` | `README.ja.md` | `日本語` |
| `ko` | `README.ko.md` | `한국어` |
| `es` | `README.es.md` | `Español` |

Every file displays these six names in this order near the top. The current locale is plain text;
the other five are links. Never replace a full self-name with a locale code or translated exonym.

## Shared Fact Ledger

Keep these facts equivalent across all six files; sentence count and word order may differ:

- Game Data Analysis Skills is a collection of pluggable Skills.
- A Skill can be used independently, combined with others, or composed by another AI/tool.
- Setup supports configurable Git hosting and independent Git/SVN/local/none planning sources.
- Query Workspace stores local/history SQL; Formal Asset Packages store shared formal assets.
- Execution surfaces are `direct`, `web`, and `manual`; missing configuration is
  `manual_required`, not a successful execution claim.
- Receipts, result lineage, Provider Snapshot, and Catalog schemas are public asset interfaces.
- The repository exposes two entry points: the Codex Skill interface and the external
  AI-agent/third-party command-and-file interface.
- The public repository contains fictional examples and generic capabilities, not production SQL,
  results, private schemas, credentials, or personal tools.
- Excel is local/offline and its bundled dependency notices remain linked.

The repository describes capability interfaces only. Do not add a product endorsement, official
site promotion, or a dependency on a separate product layer.

## Required Links

Every locale links to:

- `sql-engineering/references/execution-surfaces.md`
- `docs/READONLY_ASSET_CONSUMER_GUIDE.md`
- `docs/PUBLIC_MAINTENANCE.md`
- `excel-report-visualizer/README.md`
- `docs/INTEGRATION_INTERFACES.md`

`README.md` also carries the `public-validation.yml` badge. A link is not evidence that the target
still exists; `tools/validate_readmes.py` checks the current tree.

## Change Flow

1. Read the code/schema/reference that changed. Record the new fact, removed fact, capability
   boundary, command, and user-visible consequence.
2. Update `README.md` and `README.zh-CN.md` as factual baselines. Do not copy internal endpoints,
   project IDs, results, or source-only collaboration behavior.
3. Update `zh-TW`, `ja`, `ko`, and `es` from the same fact ledger. Write natural local-language
   documentation rather than mirroring sentence structure.
4. Keep commands, paths, schema IDs, capability IDs, placeholders, brand terms, and URLs exact.
5. Run deterministic README validation:

   ```powershell
   python .\tools\validate_readmes.py validate --root . --format json
   ```

6. Run Humanization `format=copy` once per locale:

   ```powershell
   python <humanization-skill>\scripts\check_writing.py `
     --locale <locale> --format copy <README-file> `
     --brand-term "Game Data Analysis Skills" --brand-term SQL --brand-term Deltaverse
   ```

7. Run `python tools/public_release.py validate --root .`. When README changes accompany capability
   code, also run the focused tests and capability registry validation.
8. Update `docs/README_MAINTENANCE_LOG.md` in the same commit. State the scope, affected locales,
   commands run, and intended tag.
9. Commit explicit files, push `main`, and wait for `Public validation` to pass. Only then create or
   push the release tag.

## Completion Criteria

- Six README files exist and expose the complete stable language navigation.
- Shared facts, capability boundaries, commands, links, and privacy promises agree.
- Deterministic validation, six locale checks, and public boundary validation pass.
- The maintenance log names the release commit and verification evidence.
- GitHub Actions passes for `main` and the release tag.

The log is append-only. Correct an old entry with a new dated row instead of rewriting prior release
evidence.
