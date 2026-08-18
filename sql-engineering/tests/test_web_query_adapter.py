import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "sql-engineering" / "scripts"
EXAMPLE = REPO_ROOT / "sql-engineering" / "assets" / "examples" / "web-query-adapter.deltaverse.json"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import web_query_adapter  # noqa: E402


class WebQueryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPO_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT / ".tmp")
        self.root = Path(self.temp.name)
        self.example = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deltaverse_example_is_valid(self) -> None:
        result = web_query_adapter.validate_adapter(self.example)
        self.assertEqual(result["adapter_id"], "deltaverse-da")

    def test_missing_project_adapter_requires_manual_handoff(self) -> None:
        result = web_query_adapter.resolve_adapter(self.root)
        self.assertEqual(result["status"], "manual_required")

    def test_project_local_adapter_resolves(self) -> None:
        path = self.root / ".sql-engineering" / "web-query-adapter.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.example), encoding="utf-8")
        result = web_query_adapter.resolve_adapter(self.root)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["adapter"]["adapter_id"], "deltaverse-da")

    def test_url_credentials_are_rejected(self) -> None:
        value = copy.deepcopy(self.example)
        value["entry"]["root_url"] = "https://" + "user:" + "password@" + "da.deltaverse.cn/"
        with self.assertRaises(web_query_adapter.WebQueryAdapterError):
            web_query_adapter.validate_adapter(value)

    def test_unlisted_host_is_rejected(self) -> None:
        value = copy.deepcopy(self.example)
        value["entry"]["query_url"] = "https://query.example.invalid/"
        with self.assertRaises(web_query_adapter.WebQueryAdapterError):
            web_query_adapter.validate_adapter(value)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        value = copy.deepcopy(self.example)
        value["future_selector"] = {"strategy": "css", "value": ".query"}
        with self.assertRaises(web_query_adapter.WebQueryAdapterError):
            web_query_adapter.validate_adapter(value)


if __name__ == "__main__":
    unittest.main()
