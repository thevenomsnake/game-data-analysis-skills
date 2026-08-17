import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "sql-engineering" / "scripts"
SCHEMAS_DIR = REPO_ROOT / "sql-engineering" / "schemas"
TEST_TMP_ROOT = REPO_ROOT / ".tmp"
sys.path.insert(0, str(SCRIPTS_DIR))

import formal_asset_repository as formal_repository  # noqa: E402
import project_validate  # noqa: E402
import sql_project  # noqa: E402


class SqlProjectFormalRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sql-project-formal-repository-", dir=TEST_TMP_ROOT)
        self.root = Path(self.temp.name) / "TEST_STAGE"
        self.parser = sql_project.build_parser()
        init = self.parser.parse_args(
            [
                "init",
                "--root",
                str(self.root),
                "--project-name",
                "Test Stage",
                "--project-id",
                "TEST_STAGE",
            ]
        )
        init.func(init)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def repository_summary(self) -> dict:
        return {
            "display_title": "Example Query",
            "business_topic": "Test",
            "purpose": "Verify package-backed project writes",
            "business_question": "Does the package route persist the query?",
            "base_population": "Test rows",
            "grain": "one row",
            "metrics": ["value"],
            "metric_groups": [{"name": "value"}],
            "dimensions": ["none"],
            "filters": ["none"],
            "source_logs": ["test_log"],
            "logic_summary": "Select one deterministic value.",
            "applied_criteria": [{"name": "test"}],
            "canonical_rule_status": "not_applicable",
            "canonical_rule_checks": [],
            "result_evidence": {},
        }

    def save_query(self) -> dict:
        workspace_dir = self.root / "query_workspace" / "_working" / "test"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        sql_file = workspace_dir / "candidate.sql"
        sql_file.write_text("WITH params AS (SELECT 1 AS value) SELECT value FROM params;\n", encoding="utf-8")
        spec_file = workspace_dir / "candidate.spec.json"
        spec_file.write_text(
            json.dumps({"repository_summary": self.repository_summary()}),
            encoding="utf-8",
        )
        workspace_reference = {
            "query_id": "qw-test-stage-query",
            "version": 1,
            "path": "query_workspace/20260814/qw-test-stage-query/v001.sql",
            "meta_path": "query_workspace/20260814/qw-test-stage-query/v001.meta.json",
            "formalize_seed_path": "query_workspace/20260814/qw-test-stage-query/v001.formalize_seed.json",
            "status": "runnable",
            "delivery_ready": True,
            "sql_fingerprint": "a" * 64,
            "logic_fingerprint": "b" * 64,
            "purpose": "Package route test",
            "execution_route": {},
        }
        args = self.parser.parse_args(
            [
                "save-sql",
                "--root",
                str(self.root),
                "--new-package",
                "--kind",
                "QUERY",
                "--title",
                "Example Query",
                "--sql-file",
                str(sql_file),
                "--spec-file",
                str(spec_file),
                "--status",
                "verified",
            ]
        )

        def mark_after_repository_success(root, reference, formal_path):
            self.assertTrue((root / formal_path).is_file())
            self.assertTrue(formal_path.startswith("formal_assets/FA-0001-"))

        with (
            mock.patch.object(sql_project, "validate_project_config", return_value=[]),
            mock.patch.object(sql_project, "execution_route_for_file", return_value={"status": "ready", "blockers": []}),
            mock.patch.object(sql_project, "effective_config_for_context", return_value=({}, {})),
            mock.patch.object(sql_project, "query_params_contract_problems", return_value=[]),
            mock.patch.object(sql_project, "analyze_sql_file", return_value={}),
            mock.patch.object(sql_project, "find_query_workspace_reference", return_value=workspace_reference),
            mock.patch.object(sql_project, "build_short_header", return_value=""),
            mock.patch.object(sql_project, "replace_or_prepend_short_header", side_effect=lambda kind, text, header: text),
            mock.patch.object(sql_project, "stamp_sql_generation", side_effect=lambda root, text: text),
            mock.patch.object(sql_project, "mark_query_workspace_promoted", side_effect=mark_after_repository_success),
        ):
            return args.func(args)

    def test_init_and_formal_write_lifecycle_use_packages_only(self) -> None:
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "project_manifest_v2")
        self.assertEqual(manifest["formal_asset_repository"]["index"], "formal_assets/index.json")
        self.assertEqual(manifest["packages"], [])
        for legacy in ("query_sql", "dashboard_sql", "validations", "runs", "archive"):
            self.assertFalse((self.root / legacy).exists())

        query_receipt = self.save_query()

        self.assertEqual(query_receipt["status"], "ready")
        self.assertEqual(query_receipt["package_id"], "FA-0001")
        package = formal_repository.load_package(self.root, "FA-0001")
        roles = {item["role"] for item in package["members"]}
        self.assertEqual(roles, {"query_sql", "query_spec", "query_meta"})
        self.assertTrue(all(item["path"].startswith("formal_assets/") for item in package["members"]))

        run = self.parser.parse_args(
            [
                "save-run",
                "--root",
                str(self.root),
                "--package-id",
                "FA-0001",
                "--source-artifact",
                "query-v001-sql",
                "--status",
                "skipped",
                "--user-confirmed",
                "--skip-reason",
                "User explicitly skipped execution",
                "--risk-note",
                "No result correctness evidence exists",
                "--future-verification-plan",
                "Execute this Package query before reuse",
            ]
        )
        run_receipt = run.func(run)
        self.assertEqual(run_receipt["package_revision"], 2)
        package = formal_repository.load_package(self.root, "FA-0001")
        self.assertTrue({"run_record", "run_meta"}.issubset({item["role"] for item in package["members"]}))

        update = self.parser.parse_args(
            [
                "update-artifact",
                "--root",
                str(self.root),
                "--package-id",
                "FA-0001",
                "--member-id",
                "query-v001-sql",
                "--kind",
                "QUERY",
                "--artifact-state",
                "history",
            ]
        )
        update_receipt = update.func(update)
        self.assertEqual(update_receipt["package_revision"], 3)
        package = formal_repository.load_package(self.root, "FA-0001")
        query_member = next(item for item in package["members"] if item["member_id"] == "query-v001-sql")
        self.assertEqual(query_member["lifecycle_state"], "history")

        compact = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        formal_index = json.loads((self.root / "formal_assets" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(compact["packages"], formal_index["packages"])
        self.assertEqual(compact["formal_asset_repository"]["package_count"], 1)
        self.assertNotIn("artifacts", compact)
        self.assertNotIn("run_evidence", compact)
        self.assertNotIn("query_workspace_index", compact)
        for legacy in ("query_sql", "dashboard_sql", "validations", "runs", "archive"):
            self.assertFalse((self.root / legacy).exists())

        report = project_validate.HealthReport("TEST_STAGE", self.root, False, "current")
        project_validate.validate_formal_asset_repository(report, self.root, compact)
        formal_errors = [item for item in report.payload()["errors"] if item["id"].startswith("formal_assets.")]
        self.assertEqual(formal_errors, [])

        schema = json.loads((SCHEMAS_DIR / "project_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], "project_manifest_v2")
        self.assertTrue(set(schema["required"]).issubset(compact))

    def test_legacy_archive_source_is_rejected_before_package_write(self) -> None:
        archive = self.root / "archive"
        archive.mkdir()
        source = archive / "legacy.sql"
        source.write_text("SELECT 1;\n", encoding="utf-8")
        args = self.parser.parse_args(
            [
                "save-sql",
                "--root",
                str(self.root),
                "--new-package",
                "--kind",
                "QUERY",
                "--title",
                "Legacy",
                "--sql-file",
                str(source),
            ]
        )

        with self.assertRaisesRegex(SystemExit, "legacy archive"):
            args.func(args)

        self.assertEqual(formal_repository.list_packages(self.root), [])


if __name__ == "__main__":
    unittest.main()
