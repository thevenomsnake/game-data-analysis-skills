import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PublicOnboardingTests(unittest.TestCase):
    def test_public_entrypoints_and_examples_exist(self) -> None:
        for relative in (
            "README.md",
            "README.zh-CN.md",
            "README.zh-TW.md",
            "setup/SKILL.md",
            "setup/scripts/bootstrap_repo.py",
            "setup/schemas/setup-config.json",
            "sql-engineering/SKILL.md",
            "sql-engineering/scripts/sql_workspace.py",
            "sql-engineering/assets/examples/daily-active-users.sql",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_demo_initializes_a_fictional_project_without_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            result = subprocess.run(
                [sys.executable, str(ROOT / "setup/scripts/bootstrap_repo.py"), "demo", "--root", str(workspace)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ready")
            self.assertTrue(payload["fictional"])
            project = workspace / "sql-projects" / "example"
            self.assertTrue((project / "manifest.json").is_file())
            self.assertFalse(list(project.rglob("*.csv")))
            self.assertFalse(list(project.rglob("*.xlsx")))

    def test_collaboration_route_is_local_only(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "sql-engineering/scripts/collaboration_submit.py"), "plan", "--repo-root", str(ROOT)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "local_review_only")
        self.assertNotIn("Better" + "Xml/", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
