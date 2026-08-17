from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from query_window import resolve_query_window  # noqa: E402


def configured(start: str = "2026-07-09") -> dict:
    return {
        "default_query_window": {
            "mode": "project_start_to_yesterday",
            "project_start_date": start,
            "timezone_offset": "+08:00",
            "materialization": "fixed_literals",
        }
    }


class QueryWindowTests(unittest.TestCase):
    def test_project_start_to_yesterday_is_inclusive_and_fixed(self) -> None:
        payload = resolve_query_window(configured(), as_of_date="2026-07-15")
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["source"], "project_default")
        self.assertEqual(payload["pt_start"], "2026-07-09")
        self.assertEqual(payload["pt_end"], "2026-07-14")
        self.assertEqual(payload["materialization"], "fixed_literals")

    def test_explicit_range_overrides_project_default(self) -> None:
        payload = resolve_query_window(
            configured(),
            explicit_start="2026-07-12",
            explicit_end="2026-07-13",
            as_of_date="2026-07-15",
        )
        self.assertEqual(payload["source"], "user_explicit")
        self.assertEqual(payload["pt_start"], "2026-07-12")
        self.assertEqual(payload["pt_end"], "2026-07-13")

    def test_missing_default_requires_explicit_dates(self) -> None:
        payload = resolve_query_window(
            {
                "default_query_window": {
                    "mode": "missing",
                    "project_start_date": "",
                    "timezone_offset": "+08:00",
                    "materialization": "fixed_literals",
                }
            },
            as_of_date="2026-07-15",
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("provide an explicit date range", payload["blockers"][0])

    def test_partial_explicit_range_blocks(self) -> None:
        payload = resolve_query_window(
            configured(), explicit_start="2026-07-12", as_of_date="2026-07-15"
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("provided together", payload["blockers"][0])

    def test_project_not_started_blocks(self) -> None:
        payload = resolve_query_window(configured("2026-07-20"), as_of_date="2026-07-15")
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("before project start date", payload["blockers"][0])

    def test_invalid_timezone_blocks(self) -> None:
        config = configured()
        config["default_query_window"]["timezone_offset"] = "+15:00"
        payload = resolve_query_window(config, as_of_date="2026-07-15")
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("outside the supported UTC range", payload["blockers"][0])

    def test_cli_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project_config.json").write_text(
                json.dumps(configured(), ensure_ascii=False), encoding="utf-8"
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "query_window.py"),
                    "--root",
                    str(root),
                    "--as-of-date",
                    "2026-07-15",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema_version"], "query_window_v1")
        self.assertEqual(payload["pt_end"], "2026-07-14")


if __name__ == "__main__":
    unittest.main()
