import subprocess
import tempfile
import unittest
from pathlib import Path

import bootstrap_repo


class BootstrapRepoTests(unittest.TestCase):
    def test_status_supports_an_unborn_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            result = bootstrap_repo.status(root, bootstrap_repo.DEFAULT_REMOTE)
            self.assertEqual(result["status"], "ready")
            self.assertIsNone(result["head"])
            args = type(
                "Args",
                (),
                {
                    "remote": bootstrap_repo.DEFAULT_REMOTE,
                    "git_provider": "auto",
                    "branch": "main",
                    "planning_provider": "none",
                    "planning_url": "",
                    "planning_path": None,
                    "planning_branch": "main",
                    "planning_revision": None,
                    "planning_id": "planning",
                },
            )()
            bootstrap_repo.configure_installation(args, root)
            exclude = (root / ".git/info/exclude").read_text(encoding="utf-8")
            self.assertIn("/.local/", exclude)
            self.assertFalse(bootstrap_repo.status(root, bootstrap_repo.DEFAULT_REMOTE)["dirty"])

    def test_svn_remote_is_supported_without_embedded_credentials(self) -> None:
        self.assertEqual(
            bootstrap_repo.validate_svn_remote("svn://svn.example/planning"),
            "svn://svn.example/planning",
        )
        with self.assertRaises(bootstrap_repo.BootstrapError):
            bootstrap_repo.validate_svn_remote("https://user:" + "secret@" + "svn.example/planning")

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

    def test_configuration_keeps_git_host_and_planning_provider_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = type(
                "Args",
                (),
                {
                    "remote": "https://gitlab.example/team/skills.git",
                    "git_provider": "auto",
                    "branch": "main",
                    "planning_provider": "git",
                    "planning_url": "https://git.example/team/planning.git",
                    "planning_path": None,
                    "planning_branch": "trunk",
                    "planning_revision": None,
                    "planning_id": "planning",
                },
            )()
            result = bootstrap_repo.configure_installation(args, root)
            self.assertEqual(result["git"]["provider"], "gitlab")
            self.assertEqual(result["planning_source"]["provider"], "git")
            self.assertEqual(result["planning_source"]["branch"], "trunk")
            self.assertTrue((root / ".local" / "setup-config.json").is_file())
            self.assertNotIn("password", (root / ".local" / "setup-config.json").read_text(encoding="utf-8"))

    def test_local_planning_source_requires_no_svn_or_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "planning"
            source.mkdir()
            args = type(
                "Args",
                (),
                {
                    "remote": bootstrap_repo.DEFAULT_REMOTE,
                    "git_provider": "auto",
                    "branch": "main",
                    "planning_provider": "local",
                    "planning_url": "",
                    "planning_path": source,
                    "planning_branch": "main",
                    "planning_revision": None,
                    "planning_id": "planning",
                },
            )()
            bootstrap_repo.configure_installation(args, root)
            result = bootstrap_repo.planning_sync(root)
            self.assertEqual(result["status"], "ready")
            self.assertFalse(result["mutated"])

    def test_git_planning_source_clones_into_ignored_local_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "planning.csv").write_text("id,name\n1,Example\n", encoding="utf-8")
            subprocess.run(["git", "add", "planning.csv"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "planning"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            remote = root / "planning.git"
            subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True)
            args = type(
                "Args",
                (),
                {
                    "remote": bootstrap_repo.DEFAULT_REMOTE,
                    "git_provider": "auto",
                    "branch": "main",
                    "planning_provider": "git",
                    "planning_url": str(remote),
                    "planning_path": None,
                    "planning_branch": "main",
                    "planning_revision": None,
                    "planning_id": "planning",
                },
            )()
            bootstrap_repo.configure_installation(args, root)
            result = bootstrap_repo.planning_sync(root)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["provider"], "git")
            self.assertTrue((root / ".local/planning-sources/planning/planning.csv").is_file())
            self.assertEqual(len(result["commit"]), 40)

    def test_user_managed_git_planning_checkout_is_not_updated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "planning"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            (source / "planning.txt").write_text("example\n", encoding="utf-8")
            subprocess.run(["git", "add", "planning.txt"], cwd=source, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "planning"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            args = type(
                "Args",
                (),
                {
                    "remote": bootstrap_repo.DEFAULT_REMOTE,
                    "git_provider": "auto",
                    "branch": "main",
                    "planning_provider": "git",
                    "planning_url": "",
                    "planning_path": source,
                    "planning_branch": "main",
                    "planning_revision": None,
                    "planning_id": "planning",
                },
            )()
            bootstrap_repo.configure_installation(args, root)
            result = bootstrap_repo.planning_sync(root)
            self.assertEqual(result["status"], "ready")
            self.assertFalse(result["mutated"])


if __name__ == "__main__":
    unittest.main()
