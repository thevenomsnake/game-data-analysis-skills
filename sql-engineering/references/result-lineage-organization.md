# Result Lineage Organization

Use this capability as an explicit periodic cleanup task. Do not run it during ordinary QUERY, result binding, visualization, formalization, or Dashboard delivery.

## Workflow

1. Run `result_lineage_organizer.py inspect` once. Deterministic code lists unresolved outputs, exact hashes, same-version result candidates, timestamps, workbook sheets, and duplicate files.
2. Use LLM reasoning only for business meaning. Compare the SQL facts, result columns, workbook sheets, formulas, labels, filters, grain, and coverage.
3. Discuss one case at a time with the user. A confirmation prompt must explain:
   - the concrete metric, filter, grain, mapping, and presentation differences;
   - whether the newer asset fully contains, partly overlaps, or differs from the older asset;
   - whether the output can be reproduced by a deterministic transformation of existing results;
   - the misleading outcome if the relationship is classified incorrectly;
   - the recommended bind, supersede, discard, deduplicate, or keep-for-review action.
4. Write `result_lineage_decision_v1` only from explicit user answers. A list of names or IDs is never enough evidence for confirmation.
5. Run `apply --dry-run`, inspect the receipt, then apply the same decision file. The tool changes metadata and exact duplicate files only; it never rewrites SQL or invents business meaning.
6. When an older workbook already combines one grouped result and one independently calculated overall result, first create and validate the exact `query_analysis_bundle_v1`. Then use one confirmed `adopt_bundle` action to bind the existing result ids and reusable output to that bundle without copying or rerunning them.

## Asset Semantics

- `active`: current useful result, workbook, visualization, or export.
- `superseded`: still traceable, but a named larger or corrected asset should be used instead.
- `discarded`: known-wrong or not useful as an independent asset; keep the reason in metadata.
- `needs_review`: evidence exists but product meaning is not yet settled.
- `exact_result`: one output comes directly from one result of the exact SQL version.
- `exact_results`: one output jointly consumes multiple exact result files.
- `deterministic_transform`: an output is produced by a declared filter, mapping, projection, regrouping, merge, or visualization step. The user must confirm whether metric equivalence is preserved.

`related_queries` is navigation only. It never proves that a workbook consumed another query result. A multi-result workbook must use `source_results`, `lineage_status=exact_results`, and an exact analysis-bundle reference.

When a newer asset strictly contains an older one, keep the larger active asset and mark the smaller one superseded. Byte-identical copies may be deduplicated after hash verification. A corrected filter or mapping can carry evidence forward without rerunning SQL when the user confirms there is no metric or grain risk; record the exact transformation and confirmation.

## Commands

```powershell
python .\sql-engineering\scripts\result_lineage_organizer.py inspect `
  --root .\sql-projects\DEMO_ANALYTICS `
  --format json

python .\sql-engineering\scripts\result_lineage_organizer.py apply `
  --root .\sql-projects\DEMO_ANALYTICS `
  --decision-file .\decision.json `
  --dry-run `
  --function-selection RESULT_LINEAGE_ORGANIZATION `
  --user-request "逐项确认后的历史结果整理" `
  --format json
```

An `adopt_bundle` decision action names one existing `qab-...` bundle, one reusable target attachment, and exact `grouped` and `overall` result selectors. The apply step validates both result schemas against their bundle members, updates the bundle and workbook lineage in one transaction, and retains the decision file as the audit record.
