import json
import shutil
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "sql-engineering" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import local_setup  # noqa: E402


class LocalSetupExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = REPO_ROOT
        self.project = "local-setup-test"
        self.project_root = self.repo / "sql-projects" / self.project

    def tearDown(self) -> None:
        if self.project_root.exists():
            shutil.rmtree(self.project_root)

    def test_web_surface_initialization_copies_and_validates_adapter(self) -> None:
        result = local_setup.initialize(self.repo, self.project, "starrocks", "web")
        adapter_path = self.project_root / ".sql-engineering" / "web-query-adapter.local.json"
        self.assertEqual(result["execution_surface"], "web")
        self.assertEqual(result["web_adapter"], "ready")
        self.assertTrue(adapter_path.is_file())
        self.assertEqual(json.loads(adapter_path.read_text(encoding="utf-8"))["adapter_id"], "deltaverse-da")

    def test_direct_surface_does_not_create_credentials(self) -> None:
        result = local_setup.initialize(self.repo, self.project, "starrocks", "direct")
        project_root = self.project_root
        self.assertEqual(result["execution_surface"], "manual")
        self.assertEqual(result["database"], "not_configured")
        self.assertFalse((project_root / ".sql-engineering" / "connections.local.json").exists())
