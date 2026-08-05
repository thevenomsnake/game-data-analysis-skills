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
