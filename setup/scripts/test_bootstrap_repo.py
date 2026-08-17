import subprocess
import tempfile
import unittest
from pathlib import Path

import bootstrap_repo


class BootstrapRepoTests(unittest.TestCase):
    def test_empty_checkout_and_dirty_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "README.md").write_text("public\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            remote = base / "remote.git"
            subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True)
            workspace = base / "workspace"
            workspace.mkdir()
            result = bootstrap_repo.sync(workspace, str(remote))
            self.assertEqual(result["status"], "synced")
            (workspace / "dirty.txt").write_text("local\n", encoding="utf-8")
            with self.assertRaises(bootstrap_repo.BootstrapError):
                bootstrap_repo.sync(workspace, str(remote))


if __name__ == "__main__":
    unittest.main()
