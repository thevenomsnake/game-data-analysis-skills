from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(REPO_ROOT / "tools"))
import public_sync  # noqa: E402


class PublicSyncTests(unittest.TestCase):
    def make_tree(self) -> tuple[Path, Path, tempfile.TemporaryDirectory[str]]:
        (REPO_ROOT / ".tmp").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=REPO_ROOT / ".tmp")
        root = Path(temp.name)
        source = root / "source"
        public = root / "public"
        (source / "sql-engineering/scripts").mkdir(parents=True)
        (public / "sql-engineering/scripts").mkdir(parents=True)
        manifest = {
            "schema_version": "public_sync_allowlist_v1",
            "watch_roots": ["sql-engineering/scripts"],
            "excluded_source_globs": ["sql-engineering/scripts/internal.py"],
            "allowed_public_only_globs": [],
            "exact_paths": ["sql-engineering/scripts/example.py"],
        }
        (public / "tools").mkdir()
        (public / "tools/public-sync-allowlist.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (source / "sql-engineering/scripts/example.py").write_text("print('ok')\n", encoding="utf-8")
        (public / "sql-engineering/scripts/example.py").write_text("print('ok')\n", encoding="utf-8")
        return source, public, temp

    def test_clean_exact_allowlist_passes(self) -> None:
        source, public, temp = self.make_tree()
        try:
            self.assertEqual(public_sync.audit(source, public)["status"], "pass")
        finally:
            temp.cleanup()

    def test_unreviewed_source_file_blocks(self) -> None:
        source, public, temp = self.make_tree()
        try:
            (source / "sql-engineering/scripts/new.py").write_text("print('new')\n", encoding="utf-8")
            result = public_sync.audit(source, public)
            self.assertEqual(result["status"], "block")
            self.assertIn("sql-engineering/scripts/new.py", result["source_only"])
        finally:
            temp.cleanup()

    def test_exact_drift_blocks(self) -> None:
        source, public, temp = self.make_tree()
        try:
            (public / "sql-engineering/scripts/example.py").write_text("print('changed')\n", encoding="utf-8")
            self.assertIn("sql-engineering/scripts/example.py", public_sync.audit(source, public)["exact_drift"])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
