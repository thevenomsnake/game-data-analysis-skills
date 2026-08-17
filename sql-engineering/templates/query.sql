/* @SQL_QUERY_HEADER
header_version: 1
spec_path: query_sql/<artifact-slug>/vNNN.spec.json
artifact: QUERY/<artifact-slug>/vNNN
project: <project_id>
title: <查询标题>
business_question: <这个 SQL 要回答的业务问题>
base: <统计对象/人群范围，例如 iZoneAreaID=10001 的活跃玩家>
metrics: <指标中文名，多个用、分隔>
key_filters: <关键业务筛选/排除，不写工程细节>
time_range: <业务时间范围>
output_grain: <一行结果代表什么>
result_usage: <用于核验/分析/后续看板来源>
verification_status: not_applicable
@END_SQL_QUERY_HEADER */

WITH
params AS (
    SELECT
        'YYYY-MM-DD' AS pt_start,
        'YYYY-MM-DD' AS pt_end,
        10001 AS zone_id
        -- Add ts_start/ts_end only for a real detailed-time window.
        -- Use the selected project's configured value format and bounds.
),

base AS (
    SELECT
        event_time_field AS event_time
    FROM project_resolved_physical_table AS e
    JOIN params AS p ON 1 = 1
    WHERE e.project_partition_field >= p.pt_start
      AND e.project_partition_field <= p.pt_end
      AND e.iZoneAreaID = p.zone_id
)

SELECT
    event_time
FROM base
LIMIT 100;
