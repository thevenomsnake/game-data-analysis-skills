import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_SCRIPT = SKILL_ROOT / "scripts" / "sql_workspace.py"
EXECUTE_SCRIPT = SKILL_ROOT / "scripts" / "sql_execute.py"


def run_json(script: Path, *arguments: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, str(script), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Invalid JSON output: {completed.stdout}\n{completed.stderr}") from exc
    return completed.returncode, result


class PublicExecutionTests(unittest.TestCase):
    def initialize(self, root: Path, with_environment: bool = True) -> None:
        code, result = run_json(
            WORKSPACE_SCRIPT,
            "init",
            "--root",
            str(root),
            "--project-id",
            "demo",
            "--dialect",
            "sqlite",
        )
        self.assertEqual((code, result["status"]), (0, "ready"))
        if with_environment:
            code, result = run_json(
                WORKSPACE_SCRIPT,
                "environment",
                "--root",
                str(root),
                "--name",
                "development",
                "--dialect",
                "sqlite",
                "--connection-profile",
                "development-sqlite",
                "--default",
            )
            self.assertEqual((code, result["status"]), (0, "ready"))

    def save_sql(self, root: Path, source: Path, slug: str = "sample") -> Path:
        code, result = run_json(
            WORKSPACE_SCRIPT,
            "save",
            "--root",
            str(root),
            "--sql-file",
            str(source),
            "--title",
            "Sample query",
            "--summary",
            "Reads sample rows from a configured database.",
            "--slug",
            slug,
        )
        self.assertEqual((code, result["status"]), (0, "ready"))
        return Path(result["delivery_file"])

    def write_sqlite_connection(self, root: Path, database: Path, method: str = "dbapi") -> None:
        path = root / ".sql-engineering" / "connections.local.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "sql_engineering_connections_v1",
                    "profiles": {
                        "development-sqlite": {
                            "method": method,
                            "module": "sqlite3",
                            "read_only": True,
                            "connect": {"database": str(database)},
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_cli_connection(self, root: Path, cli_script: Path) -> None:
        path = root / ".sql-engineering" / "connections.local.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "sql_engineering_connections_v1",
                    "profiles": {
                        "development-sqlite": {
                            "method": "cli",
                            "program": sys.executable,
                            "arguments": [str(cli_script)],
                            "output_format": "tsv",
                            "read_only": True,
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_dbapi_execution_writes_result_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            database = Path(temp) / "sample.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE events (category TEXT)")
                connection.executemany("INSERT INTO events VALUES (?)", [("a",), ("a",), ("b",)])
                connection.commit()
            finally:
                connection.close()
            self.initialize(root)
            self.write_sqlite_connection(root, database)
            source = Path(temp) / "query.sql"
            source.write_text(
                "SELECT category, COUNT(*) AS event_count FROM events GROUP BY category ORDER BY category;\n",
                encoding="utf-8",
            )
            saved = self.save_sql(root, source)

            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((code, result["status"]), (0, "ready"))
            self.assertEqual(result["connection_method"], "dbapi")
            self.assertEqual(result["environment"], "development")
            self.assertEqual(result["columns"], ["category", "event_count"])
            self.assertEqual(result["row_count"], 2)
            self.assertTrue(Path(result["result_file"]).is_file())
            self.assertTrue(Path(result["receipt_file"]).is_file())
            self.assertIn("a,2", Path(result["result_file"]).read_text(encoding="utf-8-sig"))

            second_code, second = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((second_code, second["status"]), (0, "ready"))
            self.assertNotEqual(second["receipt_file"], result["receipt_file"])

    def test_missing_execution_configuration_returns_manual_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.initialize(root, with_environment=False)
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1 AS value;\n", encoding="utf-8")
            saved = self.save_sql(root, source)
            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((code, result["status"]), (0, "manual_required"))
            self.assertEqual(result["delivery_file"], str(saved))
            self.assertIn("return the result file", result["next_action"])

    def test_missing_local_connection_file_returns_manual_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.initialize(root)
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1 AS value;\n", encoding="utf-8")
            saved = self.save_sql(root, source, slug="manual-query")
            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((code, result["status"]), (0, "manual_required"))
            self.assertEqual(result["reason"], "local_connection_configuration_not_found")
            self.assertEqual(result["delivery_file"], str(saved))

    def test_cli_execution_streams_and_truncates_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.initialize(root)
            cli_script = Path(temp) / "fake_cli.py"
            cli_script.write_text(
                "import sys\n"
                "query = sys.stdin.read()\n"
                "assert query.lstrip().upper().startswith('SELECT')\n"
                "print('name\\tvalue')\n"
                "print('first\\t1')\n"
                "print('second\\t2')\n",
                encoding="utf-8",
            )
            self.write_cli_connection(root, cli_script)
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1 AS value;\n", encoding="utf-8")
            saved = self.save_sql(root, source, slug="cli-query")
            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
                "--max-rows",
                "1",
            )
            self.assertEqual((code, result["status"]), (0, "ready"))
            self.assertEqual(result["connection_method"], "cli")
            self.assertEqual(result["row_count"], 1)
            self.assertTrue(result["truncated"])

    def test_browser_execution_profile_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            self.initialize(root)
            self.write_sqlite_connection(root, Path(temp) / "unused.db", method="browser")
            source = Path(temp) / "query.sql"
            source.write_text("SELECT 1 AS value;\n", encoding="utf-8")
            saved = self.save_sql(root, source)
            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((code, result["status"]), (2, "blocked"))
            self.assertIn("browser execution is not supported", result["error"])

    def test_mutating_sql_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            database = Path(temp) / "sample.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE events (category TEXT)")
                connection.execute("INSERT INTO events VALUES ('keep')")
                connection.commit()
            finally:
                connection.close()
            self.initialize(root)
            self.write_sqlite_connection(root, database)
            source = Path(temp) / "query.sql"
            source.write_text("DELETE FROM events;\n", encoding="utf-8")
            saved = self.save_sql(root, source, slug="delete-events")
            code, result = run_json(
                EXECUTE_SCRIPT,
                "run",
                "--root",
                str(root),
                "--sql-file",
                str(saved),
            )
            self.assertEqual((code, result["status"]), (2, "blocked"))
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
