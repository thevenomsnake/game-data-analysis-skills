-- Public example only. Replace demo.events and its fields with the current project contract.
WITH params AS (
    SELECT
        CAST('2025-01-01 00:00:00' AS DATETIME) AS start_ts,
        CAST('2025-01-08 00:00:00' AS DATETIME) AS end_exclusive_ts
),

active_user_days AS (
    SELECT
        CAST(e.event_time AS DATE) AS activity_date,
        e.user_id
    FROM demo.events e
    CROSS JOIN params p
    WHERE e.event_time >= p.start_ts
      AND e.event_time < p.end_exclusive_ts
      AND e.event_name = 'login'
      AND e.user_id IS NOT NULL
    GROUP BY
        CAST(e.event_time AS DATE),
        e.user_id
)

SELECT
    activity_date,
    COUNT(1) AS active_user_count
FROM active_user_days
GROUP BY activity_date
ORDER BY activity_date;
