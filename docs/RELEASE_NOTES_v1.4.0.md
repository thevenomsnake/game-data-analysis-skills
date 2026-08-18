# v1.4.0

This release makes the public edition a complete, pluggable Game Data Analysis Skills collection.

## Highlights

- Each Skill can be used independently, combined with other Skills, or used without Fairy.
- Formal projects can choose `direct`, `web`, or `manual` query execution at initialization.
- Direct execution supports read-only DB-API and CLI profiles.
- Web execution uses a validated, project-local `web_query_adapter_v1` and the user's Chrome session;
  Deltaverse is included as the current example, with a guide for adapting another site.
- Query Workspace search and Formal Asset Package discovery are documented separately, with stable
  Provider Snapshot/Catalog asset interfaces and exact receipt/result lineage.
- Excel now inspects every workbook sheet and persists schema adjustments in the local draft.
- GitHub Actions validates the public boundary, capability registry, Python compilation, Setup tests,
  and the public SQL test suite on Python 3.11 and 3.13.
- Source-to-public synchronization uses an explicit allowlist audit; production assets, private
  project data, BetterXml, and internal-only references remain excluded.
- Localized README coverage is complete for English, Simplified Chinese, Traditional Chinese,
  Japanese, Korean, and Spanish.

## Verification

- Setup tests: `7/7`
- Public SQL tests: `106/106`
- `tools/public_release.py validate`: passed
- `tools/public_sync.py audit`: passed against the maintained source tree
- Six-locale README `humanization` copy checks: passed

Released under Apache License 2.0.
