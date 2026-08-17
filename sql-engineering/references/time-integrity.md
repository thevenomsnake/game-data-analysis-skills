# Time Integrity

Use this contract when generating SQL, binding returned results, or formalizing a query whose data window may include the current local date.

## Separate Responsibilities

Keep two decisions independent:

1. `time_integrity_policy_v1` decides whether one project's paired service/client time fields must match on local calendar date before a row may enter business logic.
2. `result_time_coverage_v1` proves the actual observed result range when a fixed or runtime query window can include today.

The first decision is project/profile configured. The second is universal: a requested window is never evidence of the range actually returned.

## Project Policy

Configure the policy at project level and override it per execution profile when physical schemas differ:

```json
{
  "contract_version": "time_integrity_policy_v1",
  "mode": "required_when_event_time_or_today",
  "calendar": "gregorian",
  "date_field": "dteventdate",
  "time_field": "dtEventTime",
  "date_match": "same_local_date",
  "mismatch_action": "exclude",
  "timezone_offset": "+08:00"
}
```

Modes control paired-field filtering only:

- `report_only`: do not assume a valid field pair; still inspect returned coverage.
- `optional`: report a missing match as a warning.
- `required_when_today`: require matching when the fixed window includes today.
- `required_when_event_time_or_today`: require matching when business logic uses the event-time field or the fixed window includes today.
- `always`: require matching for every TLOG source.
- `disabled`: disable paired-field matching. Today-range evidence remains independent.

Do not copy a field pair between engines. A profile with no verified server-date field uses `report_only` until schema evidence confirms the pair.

## SQL Generation

The execution adapter owns the deterministic predicate for portable TLOG SQL. It adds the predicate beside each alias's bounded partition filter:

```sql
source.dtEventTime IS NOT NULL
AND source.dteventdate IS NOT NULL
AND CAST(source.dtEventTime AS DATE) = CAST(source.dteventdate AS DATE)
```

The partition bounds remain mandatory and sargable. The equality check is an integrity filter, not a replacement for partition pruning. Apply it to every relevant physical TLOG source; one unqualified predicate cannot validate several aliases.

Use `{{TLOG_TIME_INTEGRITY_FILTER:alias}}` only when the template needs an explicit placement. Otherwise `{{TLOG_TIME_FILTER:alias}}` receives the configured predicate automatically.

## Today Coverage Output

When a fixed or runtime query window can include today, expose the actual range after all time-integrity and business filters. The generation gate checks this before execution so a scalar query does not require a second run merely to add coverage. Prefer these stable output names:

- `实际数据开始时间`
- `实际数据结束时间`

Compute them from the already filtered base or aggregate. Do not rescan the physical source just to produce coverage. For grouped output, carry group-level minimum/maximum through the existing aggregate and obtain the overall range from those small aggregated rows when necessary.

A non-constant daily `日期` column is valid date-precision coverage. Persist `precision=date` and do not claim an intraday cutoff. Use the explicit range pair when the result is a scalar or when an exact cutoff is useful. A requested `pt_end`/`ts_end`, query execution time, download time, `CURRENT_TIMESTAMP`, or hard-coded requested date must never be labeled as the actual end.

For an empty result, the coverage status is `met_empty`: no rows is itself the observed outcome. For a non-empty result, today coverage is acceptable only when the result exposes the explicit range pair or a genuine timestamp field.

## Result Evidence

`sql_result_inspector.py` computes `result_time_coverage_v1` during the result scan. It retains only bounded metadata:

- requested fixed window and local as-of date;
- actual start/end after exclusions;
- selected output field or explicit range pair;
- valid, missing, invalid, and outside-request counts;
- at most three anomaly examples per candidate field.

Gregorian years outside `1970..2100`, malformed dates, and values outside the requested window do not participate in actual minimum/maximum values. Hijri- or Thai-calendar-like years therefore cannot stretch the observed range.

If today coverage is `not_observable` or `anomalous`, preserve the returned file as evidence but do not mark the query `result_confirmed`, save verified formal assets, or claim the result is complete. Generate a corrected immutable SQL version that exposes coverage and rerun it. Ordinary date-level analysis accepts date precision.

Historical SQL and evidence remain unchanged. Re-evaluate the current executable version when it is rerun or formalized; do not mass-edit old SQL solely to add this contract.
