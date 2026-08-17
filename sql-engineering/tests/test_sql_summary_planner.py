from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sql_query_workspace as workspace  # noqa: E402
import sql_summary_planner as planner  # noqa: E402
import project_validate  # noqa: E402
from rule_store import initialize_empty_store  # noqa: E402


GROUPED_SQL = """WITH
params AS (
    SELECT '2026-07-09' AS pt_start, '2026-07-10' AS pt_end, 10001 AS zone_id
),
base AS (
    SELECT p.PlatID AS platform, p.PlatID AS duration
    FROM demo_log.demo_dsl_playerlogin_fht0 p
    WHERE p.dtEventDate >= (SELECT pt_start FROM params)
      AND p.dtEventDate <= (SELECT pt_end FROM params)
      AND p.iZoneAreaID = (SELECT zone_id FROM params)
)
SELECT
    platform AS `平台属性`,
    COUNT(*) AS `玩家数`,
    SUM(duration) AS `时长合计`,
    AVG(duration) AS `平均时长`
FROM base
GROUP BY platform
"""

OVERALL_SQL = """WITH
params AS (
    SELECT '2026-07-09' AS pt_start, '2026-07-10' AS pt_end, 10001 AS zone_id
),
base AS (
    SELECT p.PlatID AS platform, p.PlatID AS duration
    FROM demo_log.demo_dsl_playerlogin_fht0 p
    WHERE p.dtEventDate >= (SELECT pt_start FROM params)
      AND p.dtEventDate <= (SELECT pt_end FROM params)
      AND p.iZoneAreaID = (SELECT zone_id FROM params)
)
SELECT AVG(duration) AS `平均时长`
FROM base
"""


def ok_gate() -> dict:
    return {
        "status": "ok",
        "blockers": [],
        "warnings": [],
        "checks": {},
    }


class SqlSummaryPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "EXAMPLE_TEST"
        self.root.mkdir(parents=True)
        initialize_empty_store(self.root, "EXAMPLE_TEST")
        (self.root / "project_config.json").write_text(
            json.dumps({"project_id": "EXAMPLE_TEST", "display_name": "EXAMPLE Test"}),
            encoding="utf-8",
        )
        (self.root / "manifest.json").write_text(
            json.dumps({"project_name": "EXAMPLE Test", "artifacts": [], "run_evidence": []}),
            encoding="utf-8",
        )
        self.working = self.root / "query_workspace" / "_working"
        self.working.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_grouped_mean_requires_overall_query_without_exact_components(self) -> None:
        plan = planner.build_summary_plan(GROUPED_SQL, root=self.root, group_partition="exclusive_exhaustive")

        self.assertEqual(plan["routing"], "grouped_plus_overall")
        average = next(item for item in plan["metrics"] if item["metric"] == "平均时长")
        self.assertEqual(average["feasibility"], "requires_overall_query")
        self.assertEqual(plan["overall_required_fields"], ["平均时长"])

    def test_exact_unrounded_sum_and_weight_keep_one_sql(self) -> None:
        contract = {
            "group_dimensions": ["平台属性"],
            "group_partition": "exclusive_exhaustive",
            "metrics": [
                {
                    "metric": "平均时长",
                    "semantic_type": "mean",
                    "overall_statistic": "整体平均",
                    "feasibility": "exact_with_components",
                    "grouped_fields": ["平均时长", "时长合计", "玩家数"],
                    "numerator_field": "时长合计",
                    "denominator_field": "玩家数",
                    "overall_fields": [],
                    "reason": "整体平均使用未四舍五入的时长合计除以准确玩家权重。",
                }
            ],
        }

        plan = planner.build_summary_plan(GROUPED_SQL, root=self.root, contract=contract)

        self.assertEqual(plan["routing"], "single_with_components")
        self.assertEqual(plan["overall_required_fields"], [])

    def test_matching_sum_and_non_null_count_are_detected_without_llm_contract(self) -> None:
        sql = GROUPED_SQL.replace("COUNT(*) AS `玩家数`", "COUNT(duration) AS `样本数`")

        plan = planner.build_summary_plan(sql, root=self.root, group_partition="exclusive_exhaustive")

        average = next(item for item in plan["metrics"] if item["metric"] == "平均时长")
        self.assertEqual(plan["routing"], "single_with_components")
        self.assertEqual(average["numerator_field"], "时长合计")
        self.assertEqual(average["denominator_field"], "样本数")

    def test_bucket_distribution_adds_source_level_overall_requirement(self) -> None:
        sql = """SELECT
            duration_bucket AS `时长桶`,
            COUNT(*) AS `玩家数`,
            COUNT(*) / SUM(COUNT(*)) OVER () AS `玩家占比`
        FROM player_metric
        GROUP BY duration_bucket"""

        plan = planner.build_summary_plan(sql, group_partition="exclusive_exhaustive")

        self.assertEqual(plan["routing"], "grouped_plus_overall")
        distribution = next(item for item in plan["metrics"] if item["semantic_type"] == "distribution")
        self.assertEqual(distribution["overall_fields"], ["整体时长平均值"])
        share = next(item for item in plan["metrics"] if item["metric"] == "玩家占比")
        self.assertEqual(share["feasibility"], "not_meaningful")

    def test_dual_sql_bundle_locks_params_filters_and_exact_members(self) -> None:
        grouped_source = self.working / "grouped.sql"
        overall_source = self.working / "overall.sql"
        grouped_source.write_text(GROUPED_SQL, encoding="utf-8")
        overall_source.write_text(OVERALL_SQL, encoding="utf-8")
        plan = planner.build_summary_plan(GROUPED_SQL, root=self.root, group_partition="exclusive_exhaustive")
        ready_route = {"schema_version": "execution_route_v1", "status": "ready", "blockers": []}

        with (
            mock.patch.object(workspace, "execution_route_for_file", return_value=ready_route),
            mock.patch.object(workspace, "execution_route_for_sql", return_value=ready_route),
        ):
            grouped = workspace.save_query(
                root=self.root,
                source_sql=grouped_source,
                title="平台分组平均时长",
                purpose="按平台展示玩家数量和平均时长。",
                status="runnable",
                gate=ok_gate(),
                summary_plan=plan,
                analysis_role="grouped",
            )
            overall = workspace.save_query(
                root=self.root,
                source_sql=overall_source,
                title="平台整体平均时长",
                purpose="计算与平台分组相同 Base 的整体平均时长。",
                status="runnable",
                gate=ok_gate(),
                summary_plan=plan,
                analysis_role="overall",
            )
            self.assertFalse(grouped["delivery_ready"])
            self.assertFalse(overall["delivery_ready"])

            bundle = planner.create_analysis_bundle(
                root=self.root,
                grouped_sql=grouped["path"],
                overall_sql=overall["path"],
                plan=plan,
                title="平台分组及整体平均时长",
                purpose="分别运行分组和整体查询，并共同生成一份准确可视化。",
            )

        self.assertEqual(bundle["status"], "ready")
        self.assertEqual({item["role"] for item in bundle["members"]}, {"grouped", "overall"})
        self.assertTrue(all(item["status"] == "ready" for item in bundle["delivery_receipts"]))
        stored = json.loads((self.root / bundle["path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["parameter_snapshot"]["zone_id"], "10001")
        index = workspace.load_index(self.root)
        self.assertTrue(all(entry["analysis_bundle"]["bundle_id"] == bundle["bundle_id"] for entry in index["entries"]))
        report = project_validate.HealthReport("EXAMPLE_TEST", self.root, strict=True)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        project_validate.validate_query_workspace(report, self.root, manifest)
        self.assertFalse([item for item in report.checks if item["status"] == "fail"], report.checks)


if __name__ == "__main__":
    unittest.main()
