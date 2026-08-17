/* @DASHBOARD_SQL_HEADER
header_version: 1
spec_path: dashboard_sql/<artifact-slug>/vNNN.spec.json
artifact: DASHBOARD/<artifact-slug>/vNNN
project: <project_id>
dashboard_application: <dashboard_application>
source_query: query_sql/<source-slug>/vNNN.sql
指标：<指标1、指标2>
维度：<维度1、维度2>
筛选项：无
统计周期：区间合计
time_parameters: start_date、end_date
sql_parameter_filters: 无
da_filterable_fields: 无
display_rules: <需要展示转换的字段或 无>
total_policy: sql_total=false, da_total=false
verification_status: verified | proxy_verified | unverified_skipped_run
logic_changed: false
@END_DASHBOARD_SQL_HEADER */

WITH
params AS (
    SELECT
        ${start_date} AS start_date,
        ${end_date} AS end_date
),
base_detail AS (
    SELECT
        required_dimension AS `维度`,
        required_measure AS metric_value
    FROM project_resolved_physical_table
    WHERE dialect_configured_time_or_partition_filter
)

SELECT
    `维度`,
    SUM(metric_value) AS `指标值`
FROM base_detail
GROUP BY
    `维度`;
