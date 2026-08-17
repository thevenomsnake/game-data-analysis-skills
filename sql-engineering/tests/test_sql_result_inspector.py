import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sql_result_inspector import inspect_result_file  # noqa: E402


def config() -> dict:
    return {
        "project_id": "TEST",
        "partition_policy": {
            "partition_field": "dteventdate",
            "business_time_field": "dtEventTime",
        },
        "time_integrity_policy": {
            "contract_version": "time_integrity_policy_v1",
            "mode": "required_when_event_time_or_today",
            "calendar": "gregorian",
            "date_field": "dteventdate",
            "time_field": "dtEventTime",
            "date_match": "same_local_date",
            "mismatch_action": "exclude",
            "timezone_offset": "+08:00",
        },
    }


def sql() -> str:
    return """
    WITH params AS (
        SELECT '2026-08-07' AS pt_start, '2026-08-07' AS pt_end
    )
    SELECT dteventdate, COUNT(1) AS cnt
    FROM demo_log.demo_dsl_playerlogin_fht0
    WHERE dteventdate >= (SELECT pt_start FROM params)
      AND dteventdate <= (SELECT pt_end FROM params)
    GROUP BY dteventdate
    """


class SqlResultInspectorTests(unittest.TestCase):
    def test_today_excludes_non_gregorian_and_outside_values_from_range(self) -> None:
        content = (
            "实际数据开始时间,实际数据结束时间,人数\n"
            "2026-08-07 00:00:00,2026-08-07 12:30:00,10\n"
            "2569-08-07 00:00:00,2569-08-07 12:30:00,2\n"
            "2026-08-08 00:00:00,2026-08-08 01:00:00,1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.csv"
            path.write_text(content, encoding="utf-8-sig")
            result = inspect_result_file(
                path,
                sql=sql(),
                project_config=config(),
                as_of_date="2026-08-07",
            )

        coverage = result["time_coverage"]
        self.assertEqual(coverage["status"], "observed_with_anomalies")
        self.assertEqual(coverage["requirement_status"], "anomalous")
        self.assertEqual(coverage["actual_start"], "2026-08-07 00:00:00")
        self.assertEqual(coverage["actual_end"], "2026-08-07 12:30:00")
        self.assertEqual(coverage["excluded_anomaly_count"], 4)

    def test_today_date_only_output_reports_date_precision(self) -> None:
        content = "日期,人数\n2026-08-07,10\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.csv"
            path.write_text(content, encoding="utf-8-sig")
            result = inspect_result_file(
                path,
                sql=sql(),
                project_config=config(),
                as_of_date="2026-08-07",
            )

        coverage = result["time_coverage"]
        self.assertEqual(coverage["requirement_status"], "met")
        self.assertEqual(coverage["precision"], "date")
        self.assertEqual(coverage["actual_start"], "2026-08-07")
        self.assertEqual(coverage["actual_end"], "2026-08-07")

    def test_intraday_window_excludes_same_day_values_outside_exact_bounds(self) -> None:
        detailed_sql = """
        WITH params AS (
            SELECT
                '2026-08-07' AS pt_start,
                '2026-08-07' AS pt_end,
                '2026-08-07 08:00:00' AS ts_start,
                '2026-08-07 12:00:00' AS ts_end
        )
        SELECT pl.dtEventTime AS `事件时间`
        FROM demo_log.demo_dsl_playerlogin_fht0 pl
        WHERE pl.dteventdate >= (SELECT pt_start FROM params)
          AND pl.dteventdate <= (SELECT pt_end FROM params)
          AND pl.dtEventTime >= (SELECT ts_start FROM params)
          AND pl.dtEventTime <= (SELECT ts_end FROM params)
          AND CAST(pl.dtEventTime AS DATE) = CAST(pl.dteventdate AS DATE)
        """
        content = (
            "事件时间,人数\n"
            "2026-08-07 07:59:59,1\n"
            "2026-08-07 08:00:00,1\n"
            "2026-08-07 12:00:00,1\n"
            "2026-08-07 12:00:01,1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.csv"
            path.write_text(content, encoding="utf-8-sig")
            result = inspect_result_file(
                path,
                sql=detailed_sql,
                project_config=config(),
                as_of_date="2026-08-07",
            )

        coverage = result["time_coverage"]
        self.assertEqual(coverage["precision"], "datetime")
        self.assertEqual(coverage["actual_start"], "2026-08-07 08:00:00")
        self.assertEqual(coverage["actual_end"], "2026-08-07 12:00:00")
        self.assertEqual(coverage["excluded_anomaly_count"], 2)

    def test_historical_range_is_observed_without_today_requirement(self) -> None:
        content = "日期,人数\n2026-08-06,10\n"
        historical_sql = sql().replace("2026-08-07", "2026-08-06")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.csv"
            path.write_text(content, encoding="utf-8-sig")
            result = inspect_result_file(
                path,
                sql=historical_sql,
                project_config=config(),
                as_of_date="2026-08-07",
            )

        self.assertEqual(result["time_coverage"]["requirement_status"], "not_required")
        self.assertEqual(result["time_coverage"]["actual_start"], "2026-08-06")


if __name__ == "__main__":
    unittest.main()
