import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import requirement_intake  # noqa: E402


def write_project_config(root: Path) -> None:
    (root / "project_config.json").write_text(
        json.dumps(
            {
                "project_id": "DEMO_ANALYTICS",
                "display_name": "DEMO-ANALYTICS",
                "sql_dialect": "Hive",
                "query_engine": "Hive",
                "default_query_window": {
                    "mode": "project_start_to_yesterday",
                    "project_start_date": "2026-07-09",
                    "timezone_offset": "+08:00",
                    "materialization": "fixed_literals",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class RequirementIntakeTests(unittest.TestCase):
    @staticmethod
    def duration_rule_context(*, fixed_mode_values: list[int] | None = None) -> dict:
        hard_constraints = [
            {
                "type": "requires_explicit_business_decision",
                "decision_key": "mode_scope",
                "allowed_semantics": ["game_overall", "configured_mode_categories"],
                "reason": "Duration changes with mode scope.",
                "rule_id": "obt-game-total-active-duration",
                "concept_key": "game-total-active-duration",
                "title": "Duration rule",
            }
        ]
        if fixed_mode_values:
            hard_constraints.append(
                {
                    "type": "allowed_values",
                    "field": "GameMode",
                    "values": fixed_mode_values,
                    "rule_id": "specific-duration-rule",
                    "concept_key": "specific-duration-rule",
                }
            )
        return {
            "rule_application": {"application_sha256": "a" * 64},
            "active_rules": [
                {
                    "rule_id": "obt-game-total-active-duration",
                    "concept_key": "game-total-active-duration",
                    "title": "Duration rule",
                    "decision_question": requirement_intake.MODE_SCOPE_QUESTION,
                }
            ],
            "hard_constraints": hard_constraints,
        }

    def test_clear_query_with_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project_config(root)
            payload = requirement_intake.classify(
                "DEMO-ANALYTICS 看 2026-06-01 到 2026-06-07 每天新增用户，iZoneAreaID=10001",
                project_root=root,
            )
        self.assertTrue(payload["is_data_query_request"])
        self.assertEqual(payload["route_hint"], "QUERY")
        self.assertEqual(payload["clarity"], "clear_query")
        self.assertEqual(payload["decision"], "handoff_to_query")
        self.assertEqual(payload["missing_slots"], [])
        self.assertIn("iZoneAreaID=10001", payload["extracted"]["filters"])

    def test_partial_query_reports_blocking_questions(self) -> None:
        payload = requirement_intake.classify("DEMO-ANALYTICS 按天看新增用户")
        self.assertTrue(payload["is_data_query_request"])
        self.assertEqual(payload["route_hint"], "QUERY")
        self.assertEqual(payload["clarity"], "partially_clear")
        self.assertIn("time_range", payload["missing_slots"])
        self.assertIn("统计哪个时间范围？", payload["blocking_questions"])

    def test_project_default_supplies_missing_query_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project_config(root)
            payload = requirement_intake.classify("DEMO-ANALYTICS 按天看新增用户", project_root=root)
        self.assertEqual(payload["clarity"], "clear_query")
        self.assertNotIn("time_range", payload["missing_slots"])
        self.assertEqual(payload["extracted"]["time_range"]["source"], "project_default")
        self.assertEqual(payload["extracted"]["time_range"]["pt_start"], "2026-07-09")

    def test_review_request_routes_to_review(self) -> None:
        payload = requirement_intake.classify("帮我 review 这个目录里的 SQL，看看口径和性能")
        self.assertFalse(payload["is_data_query_request"])
        self.assertEqual(payload["route_hint"], "REVIEW")
        self.assertEqual(payload["decision"], "route_non_query")

    def test_dashboard_request_does_not_enter_query(self) -> None:
        payload = requirement_intake.classify("把这个查询转成 DA 看板 SQL，保留筛选项")
        self.assertFalse(payload["is_data_query_request"])
        self.assertEqual(payload["route_hint"], "DASHBOARD")
        self.assertEqual(payload["clarity"], "not_query")

    def test_metric_definition_question_routes_to_rules(self) -> None:
        payload = requirement_intake.classify("新增用户口径是什么")
        self.assertFalse(payload["is_data_query_request"])
        self.assertEqual(payload["route_hint"], "RULES")
        self.assertEqual(payload["decision"], "route_non_query")

    def test_tlog_owner_lookup_routes_to_knowledge_before_source_intake(self) -> None:
        owner = requirement_intake.classify("Damage 这个 tlog 哪个 QA 负责")
        source = requirement_intake.classify("同步 TLog XML 并生成字段目录")

        self.assertEqual(owner["route_hint"], "KNOWLEDGE")
        self.assertEqual(source["route_hint"], "SOURCE_INTAKE")

    def test_duration_without_mode_scope_blocks_before_query_generation(self) -> None:
        business_decisions = requirement_intake.resolve_business_decisions(
            self.duration_rule_context(),
            "DEMO-ANALYTICS 查询玩家累计游戏时长",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project_config(root)
            with mock.patch.object(
                requirement_intake,
                "evaluate_project_business_decisions",
                return_value=business_decisions,
            ):
                payload = requirement_intake.classify(
                    "DEMO-ANALYTICS 查询 2026-07-09 玩家累计游戏时长总计",
                    project_root=root,
                )

        self.assertEqual(payload["business_decisions"]["status"], "needs_input")
        self.assertIn("business_decision:mode_scope", payload["missing_slots"])
        self.assertIn(requirement_intake.MODE_SCOPE_QUESTION, payload["blocking_questions"])
        self.assertEqual(payload["decision"], "ask_clarifying_question")

    def test_bare_regular_resolves_to_regular_plus_activity(self) -> None:
        payload = requirement_intake.resolve_business_decisions(
            self.duration_rule_context(),
            "DEMO-ANALYTICS 查询常规模式累计游戏时长",
        )

        self.assertEqual(payload["status"], "resolved")
        value = payload["required"][0]["value"]
        self.assertEqual(value["categories"], ["常规", "活动"])
        self.assertEqual(value["normalization"], "bare_regular_includes_activity")

    def test_pure_regular_does_not_include_activity(self) -> None:
        payload = requirement_intake.resolve_business_decisions(
            self.duration_rule_context(),
            "DEMO-ANALYTICS 查询纯常规累计游戏时长",
        )

        self.assertEqual(payload["required"][0]["value"]["categories"], ["常规"])

    def test_clarification_resolves_only_the_existing_mode_decision(self) -> None:
        payload = requirement_intake.resolve_business_decisions(
            self.duration_rule_context(),
            "DEMO-ANALYTICS 查询玩家累计游戏时长",
            "常规",
        )

        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["required"][0]["value"]["source"], "clarification")
        self.assertEqual(payload["required"][0]["value"]["categories"], ["常规", "活动"])

    def test_more_specific_confirmed_mode_rule_avoids_redundant_question(self) -> None:
        payload = requirement_intake.resolve_business_decisions(
            self.duration_rule_context(fixed_mode_values=[10, 23]),
            "DEMO-ANALYTICS 查询玩家累计游戏时长",
        )

        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["required"][0]["value"]["scope_type"], "fixed_by_confirmed_rule")

    def test_cli_emits_json(self) -> None:
        script = SCRIPTS_DIR / "requirement_intake.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--text",
                "DEMO-ANALYTICS 看 2026-06-01 每天新增用户，区服 10001",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_version"], "requirement_intake_v2")
        self.assertEqual(payload["route_hint"], "QUERY")


if __name__ == "__main__":
    unittest.main()
