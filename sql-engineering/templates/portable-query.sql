WITH params AS (
    SELECT
        '{{PT_START}}' AS pt_start,
        '{{PT_END}}' AS pt_end,
        10001 AS zone_id
),
base AS (
    SELECT
        src.vOpenID
    FROM {{TLOG:PlayerLogin:src}}
    WHERE {{TLOG_TIME_FILTER:src}}
      AND src.iZoneAreaID = (SELECT zone_id FROM params)
)
SELECT
    COUNT(DISTINCT vOpenID) AS user_cnt
FROM base
