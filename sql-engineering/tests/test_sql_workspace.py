import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sql_workspace.py"
EXAMPLE_SQL = Path(__file__).resolve().parents[1] / "assets" / "examples" / "daily-active-users.sql"


class PublicWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        namespace = {"__name__": "sql_workspace_test_module", "__file__": str(SCRIPT)}
        exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), namespace)
        cls.module = type("Module", (), namespace)

    def test_save_versions_searches_and_returns_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            source = Path(temp) / "incoming.sql"
            source.write_text("SELECT 1 AS value;\n", encoding="utf-8")
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": False})
            )
            args = type(
                "Args",
                (),
                {
                    "root": str(root),
                    "sql_file": str(source),
                    "title": "Daily active users",
                    "summary": "Counts distinct active users by date.",
                    "kind": "temporary",
                    "slug": "daily-active-users",
                    "tag": ["activity"],
                },
            )
            first = self.module.command_save(args)
            second = self.module.command_save(args)
            self.assertEqual(first["status"], "ready")
            self.assertTrue(first["delivery_file"].endswith("v001.sql"))
            self.assertTrue(second["delivery_file"].endswith("v002.sql"))
            self.assertEqual(source.read_text(encoding="utf-8"), "SELECT 1 AS value;\n")

            search = self.module.command_search(type("Args", (), {"root": str(root), "query": "active"}))
            self.assertEqual(len(search["matches"]), 2)
            self.assertTrue(Path(search["matches"][0]["absolute_path"]).is_absolute())

            meta = json.loads(Path(first["delivery_file"]).with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["source"]["file_name"], "incoming.sql")
            self.assertNotIn(str(Path(temp)), json.dumps(meta))

    def test_bootstrap_creates_repository_layout_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            args = type(
                "Args",
                (),
                {"root": str(root), "project_id": "example", "dialect": "starrocks"},
            )
            first = self.module.command_bootstrap(args)
            second = self.module.command_bootstrap(args)

            self.assertEqual(first["status"], "ready")
            self.assertEqual(second["status"], "ready")
            self.assertEqual(second["project"]["status"], "existing")
            for directory in ("_asset_catalog", "_review_inbox", "_rule_review"):
                self.assertTrue((root / "sql-projects" / directory / ".gitkeep").is_file())
            self.assertTrue((root / "sql-projects" / "example" / ".sql-engineering" / "project.json").is_file())
            self.assertTrue((root / "sql-projects" / "example" / "sql-workspace" / "index.json").is_file())
            self.assertTrue((root / "sql-projects" / "example" / "sources" / "source-catalog.json").is_file())
            self.assertTrue((root / "sql-projects" / "example" / "knowledge" / "knowledge-catalog.json").is_file())
            self.assertTrue((root / "sql-projects" / "example" / "rules" / "rule-catalog.json").is_file())

    def test_receipt_blocks_modified_saved_sql(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1;\n", encoding="utf-8")
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "hive", "force": False})
            )
            saved = self.module.command_save(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "sql_file": str(source),
                        "title": "One",
                        "summary": "Returns one scalar.",
                        "kind": "temporary",
                        "slug": "one",
                        "tag": [],
                    },
                )
            )
            saved_path = Path(saved["delivery_file"])
            saved_path.write_text("SELECT 2;\n", encoding="utf-8")
            receipt = self.module.delivery_receipt(root, saved_path)
            self.assertEqual(receipt["status"], "blocked")
            self.assertIn("metadata_hash_mismatch", receipt["blockers"])
            self.assertIn("index_hash_mismatch", receipt["blockers"])

    def test_environment_configuration_is_saved_with_sql_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1;\n", encoding="utf-8")
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": False})
            )
            environment = self.module.command_environment(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "name": "development",
                        "dialect": "starrocks",
                        "connection_profile": "development-starrocks",
                        "default": True,
                    },
                )
            )
            saved = self.module.command_save(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "sql_file": str(source),
                        "title": "One",
                        "summary": "Returns one scalar.",
                        "kind": "temporary",
                        "slug": "one",
                        "tag": [],
                        "environment": "",
                    },
                )
            )
            meta = json.loads(Path(saved["delivery_file"]).with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual(environment["default_environment"], "development")
            self.assertEqual(meta["execution_environment"], "development")
            self.assertEqual(meta["dialect"], "starrocks")
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": True})
            )
            _, repaired_config, _ = self.module.load_project(root)
            self.assertEqual(repaired_config["execution"]["default_environment"], "development")

    def test_project_context_paths_must_be_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": False})
            )
            config_path = root / ".sql-engineering" / "project.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["context_paths"] = ["C:/Users/example/private-schema.md"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "project-relative"):
                self.module.load_project(root)

    def test_project_sources_knowledge_rules_and_status_are_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": False})
            )
            telemetry = Path(temp) / "events.xml"
            telemetry.write_text("<events><event name='PlayerLogin'/></events>\n", encoding="utf-8")
            source_args = type(
                "Args",
                (),
                {
                    "root": str(root),
                    "file": str(telemetry),
                    "name": "PlayerLogin event definition",
                    "description": "Raw telemetry contract for login events.",
                    "source_format": "xml",
                    "slug": "player-login",
                },
            )
            source = self.module.command_source(source_args)
            duplicate = self.module.command_source(source_args)
            self.assertEqual(source["source_id"], "player-login:v001")
            self.assertEqual(duplicate["registration_status"], "existing")
            self.assertEqual(Path(source["source_file"]).read_bytes(), telemetry.read_bytes())

            planning = Path(temp) / "mode-table.csv"
            planning.write_text("mode_id,mode_name\n7,Tutorial\n", encoding="utf-8")
            planning_result = self.module.command_knowledge(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "file": str(planning),
                        "kind": "planning",
                        "name": "Game mode planning table",
                        "description": "Original mode mapping supplied by design.",
                        "slug": "game-mode-table",
                        "confirmed_by": "",
                        "confirmation_note": "",
                        "based_on": [],
                    },
                )
            )
            with self.assertRaisesRegex(ValueError, "evidence, not confirmed knowledge"):
                self.module.command_knowledge(
                    type(
                        "Args",
                        (),
                        {
                            "root": str(root),
                            "file": str(planning),
                            "kind": "planning",
                            "name": "Incorrectly confirmed planning table",
                            "description": "Must remain source evidence.",
                            "slug": "incorrect-planning",
                            "confirmed_by": "analyst@example",
                            "confirmation_note": "This is not allowed.",
                            "based_on": [],
                        },
                    )
                )
            confirmed = Path(temp) / "confirmed-mode.json"
            confirmed.write_text('{"mode_id": 7, "mode_name": "Tutorial"}\n', encoding="utf-8")
            confirmed_result = self.module.command_knowledge(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "file": str(confirmed),
                        "kind": "confirmed",
                        "name": "Confirmed mode mapping",
                        "description": "Human-reviewed mapping used by SQL.",
                        "slug": "game-mode-mapping",
                        "confirmed_by": "analyst@example",
                        "confirmation_note": "Confirmed against the current planning table.",
                        "based_on": [planning_result["knowledge_id"]],
                    },
                )
            )
            self.assertEqual(planning_result["knowledge_id"], "planning:game-mode-table:v001")
            self.assertEqual(confirmed_result["knowledge_id"], "confirmed:game-mode-mapping:v001")

            rule_input = Path(temp) / "rule.json"
            rule_body = {
                "schema_version": "sql_rule_input_v1",
                "concept_key": "daily_active_user",
                "title": "Daily active user",
                "business_definition": "Distinct players with a login event on the selected date.",
                "grain": "activity_date",
                "calculation": {"aggregation": "count_distinct", "entity": "player_id"},
                "filters": [{"field": "zone_id", "operator": "=", "value": 42}],
                "source_contracts": [source["source_id"]],
                "knowledge_contracts": [confirmed_result["knowledge_id"]],
            }
            rule_input.write_text(json.dumps(rule_body), encoding="utf-8")
            invalid_rule = dict(rule_body)
            invalid_rule["source_contracts"] = ["missing-source:v001"]
            rule_input.write_text(json.dumps(invalid_rule), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown contracts"):
                self.module.command_rule(
                    type(
                        "Args",
                        (),
                        {
                            "root": str(root),
                            "rule_file": str(rule_input),
                            "confirmed_by": "analyst@example",
                            "confirmation_note": "This reference must be rejected.",
                        },
                    )
                )
            rule_input.write_text(json.dumps(rule_body), encoding="utf-8")
            rule = self.module.command_rule(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "rule_file": str(rule_input),
                        "confirmed_by": "analyst@example",
                        "confirmation_note": "Approved for the example project.",
                    },
                )
            )
            self.assertEqual(rule["version"], "v001")
            rule_body["business_definition"] = "Distinct eligible players with a login event on the selected date."
            rule_input.write_text(json.dumps(rule_body), encoding="utf-8")
            revised = self.module.command_rule(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "rule_file": str(rule_input),
                        "confirmed_by": "analyst@example",
                        "confirmation_note": "Updated definition approved after review.",
                    },
                )
            )
            self.assertEqual(revised["version"], "v002")
            self.assertTrue((root / "rules" / "definitions" / "daily_active_user" / "v001.json").is_file())
            self.assertTrue((root / "rules" / "definitions" / "daily_active_user" / "v002.json").is_file())

            status = self.module.command_status(type("Args", (), {"root": str(root)}))
            self.assertTrue(status["query_context_ready"])
            self.assertEqual(status["raw_source_count"], 1)
            self.assertEqual(status["planning_knowledge_count"], 1)
            self.assertEqual(status["confirmed_knowledge_count"], 1)
            self.assertEqual(status["canonical_rule_count"], 1)

    def test_bundled_example_saves_and_returns_ready_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.module.command_init(
                type("Args", (), {"root": str(root), "project_id": "demo", "dialect": "starrocks", "force": False})
            )
            saved = self.module.command_save(
                type(
                    "Args",
                    (),
                    {
                        "root": str(root),
                        "sql_file": str(EXAMPLE_SQL),
                        "title": "Daily active users",
                        "summary": "Counts distinct login users by activity date.",
                        "kind": "temporary",
                        "slug": "daily-active-users",
                        "tag": ["activity"],
                    },
                )
            )
            receipt = self.module.delivery_receipt(root, Path(saved["delivery_file"]))
            self.assertEqual(receipt["status"], "ready")
            self.assertTrue(Path(receipt["delivery_file"]).is_absolute())
            self.assertEqual(
                Path(receipt["delivery_file"]).read_text(encoding="utf-8"),
                EXAMPLE_SQL.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
