from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from rule_store import initialize_empty_store  # noqa: E402
from sql_facts import execution_fingerprint  # noqa: E402


SOURCE_SQL = """-- Release smoke: count distinct login players in one fixed day and zone.
WITH params AS (
    SELECT
        '2026-07-09' AS pt_start,
        '2026-07-09' AS pt_end,
        10001 AS zone_id
)
SELECT
    COUNT(DISTINCT p.vOpenID) AS `玩家数`,
    MIN(p.dteventdate) AS `实际数据开始时间`,
    MAX(p.dteventdate) AS `实际数据结束时间`
FROM demo_log.demo_dsl_playerlogin_fht0 p
WHERE p.dteventdate >= (SELECT pt_start FROM params)
  AND p.dteventdate <= (SELECT pt_end FROM params)
  AND p.iZoneAreaID = (SELECT zone_id FROM params)
"""


def project_config() -> dict:
    return {
        "version": 2,
        "project_id": "RELEASE_SMOKE",
        "display_name": "SQL Engineering Release Smoke",
        "sql_dialect": "StarRocks",
        "query_engine": "StarRocks",
        "query_environment": {
            "name": "Release Smoke StarRocks",
            "status": "configured",
            "notes": "Deterministic local fixture; no database connection.",
        },
        "dashboard_application": {
            "name": "DA",
            "status": "configured",
            "notes": "Deterministic local fixture.",
        },
        "table_naming_profile": {
            "name": "demo_starrocks",
            "dialect": "StarRocks",
            "database": "demo_log",
            "pattern": "demo_log.demo_dsl_{log_lower}_fht0",
            "description": "Release smoke table profile.",
            "status": "configured",
        },
        "partition_policy": {
            "name": "demo_log_dt_event_date",
            "required_for_tlog": True,
            "partition_field": "dteventdate",
            "partition_format": "date_or_datetime",
            "partition_bounds": "inclusive",
            "whole_day_filter_mode": "partition_only",
            "business_time_field": "dtEventTime",
            "business_time_required": False,
            "business_time_required_when": "detailed_time_logic",
            "detail_time_bounds": "inclusive",
            "strict_generation": True,
            "requires_schema_confirmation": True,
        },
        "default_query_window": {
            "mode": "project_start_to_yesterday",
            "project_start_date": "2026-07-09",
            "timezone_offset": "+08:00",
            "materialization": "fixed_literals",
        },
        "table_overrides": {},
        "generation_contract": {
            "strict_dialect_rules": True,
            "require_query_environment_for_query": True,
            "require_dashboard_application_for_dashboard": True,
            "block_formal_sql_when_config_missing": True,
        },
    }


class ReleaseLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = REPO_ROOT / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sql-release-", dir=tmp_root)
        self.sandbox = Path(self.temp.name)
        self.root = self.sandbox / "project"
        self.root.mkdir()
        (self.root / "sources").mkdir()
        self._write_json(self.root / "project_config.json", project_config())
        self._write_json(
            self.root / "sources" / "xml_catalog.json",
            {
                "logs": [
                    {
                        "name": "PlayerLogin",
                        "desc": "玩家登录",
                        "fields": [
                            {"name": "dteventdate", "type": "date"},
                            {"name": "dtEventTime", "type": "datetime"},
                            {"name": "iZoneAreaID", "type": "int"},
                            {"name": "vOpenID", "type": "string"},
                        ],
                    }
                ]
            },
        )
        initialize_empty_store(self.root, "RELEASE_SMOKE")
        self._write_json(
            self.root / "manifest.json",
            {
                "project_name": "SQL Engineering Release Smoke",
                "project_config_file": "project_config.json",
                "artifact_counters": {"QUERY": {}, "VALIDATION": {}, "DASHBOARD": {}},
                "artifacts": [],
                "run_evidence": [],
                "canonical_rule_store": {
                    "contract_version": "canonical_rule_store_v2",
                    "store": "rules/store.json",
                    "activation_index": "rules/activation-index.json",
                    "definitions_root": "rules/definitions",
                },
            },
        )
        self.external_sql = self.sandbox / "external.sql"
        self.external_sql.write_text(SOURCE_SQL, encoding="utf-8")
        self.external_bytes = self.external_sql.read_bytes()
        self.result_csv = self.sandbox / "result.csv"
        self.result_csv.write_text(
            "玩家数,实际数据开始时间,实际数据结束时间\n"
            "42,2026-07-09,2026-07-09\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_script(
        self,
        script: str,
        *arguments: str,
        expected_code: int = 0,
    ) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "DA_SKILLS_LDAP_USERNAME": "release-smoke",
                "PYTHONUTF8": "1",
            }
        )
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / script), *arguments],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected_code,
            f"{script} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"{script} did not return JSON: {exc}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )

    def formal_file_hashes(self) -> dict[str, str]:
        roots = ["query_sql", "validation_sql", "dashboard_sql", "runs", "reviews"]
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in roots
            for path in sorted((self.root / name).rglob("*"))
            if path.is_file()
        }

    def test_query_result_formalize_dashboard_lifecycle(self) -> None:
        request = "[QUERY] release smoke login player count"
        imported = self.run_script(
            "sql_query_workspace.py",
            "import",
            "--root",
            str(self.root),
            "--sql-file",
            str(self.external_sql),
            "--title",
            "发布门禁登录玩家数",
            "--purpose",
            "导入外部登录玩家 SQL，并在项目工作区内完成发布流程验证。",
            "--business-question",
            "指定日期和区服内有多少去重登录玩家？",
            "--format",
            "json",
            "--function-selection",
            "QUERY",
            "--user-request",
            request,
        )
        self.assertTrue(imported["source_unchanged"])
        working_copy = self.root / imported["working_copy_path"]
        working_copy.write_text(
            working_copy.read_text(encoding="utf-8")
            + "\n-- Project-local release candidate.\n",
            encoding="utf-8",
        )

        saved = self.run_script(
            "sql_query_workspace.py",
            "save",
            "--root",
            str(self.root),
            "--sql-file",
            str(working_copy),
            "--title",
            "发布门禁登录玩家数",
            "--purpose",
            "统计指定日期和区服内 PlayerLogin 的去重登录玩家数。",
            "--business-question",
            "指定日期和区服内有多少去重登录玩家？",
            "--status",
            "runnable",
            "--query-id",
            imported["query_id"],
            "--source-kind",
            "user_provided",
            "--usage-class",
            "reusable_analysis",
            "--revision-note",
            "确认项目内候选 SQL 可执行并进入结果验证。",
            "--format",
            "json",
            "--function-selection",
            "QUERY",
            "--user-request",
            request,
        )
        self.assertTrue(saved["delivery_ready"])
        self.assertEqual(saved["delivery_receipt"]["status"], "ready")
        self.assertEqual(saved["version"], 2)
        saved_sql = self.root / saved["path"]
        first_line = saved_sql.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("@SQL_GENERATION", first_line)
        self.assertRegex(first_line, r"generated_by_ldap=[A-Za-z0-9][A-Za-z0-9._-]*$")
        self.assertEqual(saved["execution_route"]["sql_dialect"], "StarRocks")

        receipt = self.run_script(
            "sql_query_workspace.py",
            "receipt",
            "--root",
            str(self.root),
            "--sql-path",
            saved["path"],
            "--format",
            "json",
        )
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(
            receipt["execution_route"]["sql_fingerprint"],
            saved["sql_fingerprint"],
        )

        attached = self.run_script(
            "sql_query_workspace.py",
            "attach-output",
            "--root",
            str(self.root),
            "--sql-path",
            saved["path"],
            "--file",
            str(self.result_csv),
            "--kind",
            "result_evidence",
            "--source-kind",
            "user_result",
            "--title",
            "发布门禁结果",
            "--purpose",
            "绑定发布门禁查询返回的精确 CSV 结果证据。",
            "--format",
            "json",
            "--function-selection",
            "QUERY",
            "--user-request",
            request,
        )
        attached_result = self.root / attached["path"]
        self.assertTrue(attached_result.is_file())

        confirmed = self.run_script(
            "sql_query_workspace.py",
            "mark",
            "--root",
            str(self.root),
            "--sql-path",
            saved["path"],
            "--status",
            "result_confirmed",
            "--reason",
            "发布门禁结果字段和数值已确认。",
            "--result-status",
            "passed",
            "--format",
            "json",
            "--function-selection",
            "QUERY",
            "--user-request",
            request,
        )
        self.assertEqual(confirmed["query_status"], "result_confirmed")

        formalized = self.run_script(
            "sql_formalize.py",
            "--root",
            str(self.root),
            "--source-sql",
            str(saved_sql),
            "--result-file",
            str(attached_result),
            "--target",
            "query-dashboard",
            "--title",
            "发布门禁登录玩家数",
            "--slug",
            "release-login-players",
            "--user-confirmed",
            "--confirmed-by",
            "release-smoke",
            "--semantic-mode",
            "deterministic",
            "--use-fact-bundle",
            "auto",
            "--refresh-viewers",
            "incremental",
            "--format",
            "json",
            "--function-selection",
            "SQL_FORMALIZE",
            "--user-request",
            "[SQL_FORMALIZE] formalize the confirmed release smoke result",
        )
        self.assertEqual(formalized["status"], "saved", formalized.get("blockers"))
        self.assertEqual(formalized["delivery_receipt"]["status"], "ready")

        package_manifest_path = next((self.root / "formal_assets").glob("*/manifest.json"))
        package_root = package_manifest_path.parent
        package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
        current_ids = set(package_manifest["current"]["member_ids"])
        current_members = [
            item for item in package_manifest["members"] if item["member_id"] in current_ids
        ]
        current_roles = {item["role"] for item in current_members}
        self.assertTrue({"formal_query", "validation_sql", "dashboard_sql"} <= current_roles)

        def member_with_role(role: str) -> dict:
            return next(item for item in current_members if item["role"] == role)

        query_member = member_with_role("formal_query")
        validation_member = member_with_role("validation_sql")
        dashboard_member = member_with_role("dashboard_sql")
        query = json.loads((self.root / member_with_role("query_meta")["path"]).read_text(encoding="utf-8"))
        validation = json.loads((self.root / member_with_role("validation_meta")["path"]).read_text(encoding="utf-8"))
        dashboard = json.loads((self.root / member_with_role("dashboard_meta")["path"]).read_text(encoding="utf-8"))
        self.assertEqual(dashboard["linked_query"], query["path"])
        self.assertEqual(dashboard["linked_validation"], validation["path"])
        run = json.loads((self.root / member_with_role("run_meta")["path"]).read_text(encoding="utf-8"))
        self.assertEqual(run["source_artifact"], query["path"])
        self.assertEqual(
            run["result_columns"],
            ["玩家数", "实际数据开始时间", "实际数据结束时间"],
        )
        self.assertTrue((package_root / "members" / run["path"]).is_file())
        self.assertTrue((package_root / "members" / run["evidence_file"]).is_file())

        for member in current_members:
            if not member["role"].endswith(("_sql", "_spec", "_meta")) and member["role"] not in {"formal_query"}:
                continue
            sql_path = self.root / member["path"]
            self.assertTrue(sql_path.is_file(), member)
        query_spec = json.loads((self.root / member_with_role("query_spec")["path"]).read_text(encoding="utf-8"))
        fact_fingerprint = query_spec["formalize_bundle"]["sql_facts"][
            "execution_fingerprint"
        ]
        self.assertEqual(
            run["source_sql_fingerprint"],
            execution_fingerprint((self.root / query_member["path"]).read_text(encoding="utf-8")),
        )
        self.assertEqual(
            query["origin_query_workspace"]["source_sql_fingerprint"],
            fact_fingerprint,
        )

        receipt_manifest = self.root / formalized["saved_outputs"]["package_manifest"]
        self.assertTrue(receipt_manifest.is_file())
        self.assertEqual(receipt_manifest.parent.parent, package_root)
        self.assertEqual(formalized["saved_outputs"]["viewer_refresh_mode"], "explicit_shared_projection")
        self.assertTrue((self.root / "formal_assets" / "index.json").is_file())
        self.assertEqual(self.external_sql.read_bytes(), self.external_bytes)

        manifest_path = self.root / "manifest.json"
        manifest_before = manifest_path.read_bytes()
        files_before = self.formal_file_hashes()
        blocked = self.run_script(
            "sql_formalize.py",
            "--root",
            str(self.root),
            "--source-sql",
            str(saved_sql),
            "--result-file",
            str(self.sandbox / "missing-result.csv"),
            "--target",
            "query-dashboard",
            "--title",
            "不得写入的失败事务",
            "--slug",
            "blocked-release-transaction",
            "--user-confirmed",
            "--format",
            "json",
            "--function-selection",
            "SQL_FORMALIZE",
            "--user-request",
            "[SQL_FORMALIZE] verify blocked transaction isolation",
            expected_code=1,
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.formal_file_hashes(), files_before)


if __name__ == "__main__":
    unittest.main()
