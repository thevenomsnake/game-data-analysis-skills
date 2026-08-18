from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import validate_readmes


REPO_ROOT = Path(__file__).resolve().parents[1]


class ValidateReadmesTests(unittest.TestCase):
    def make_tree(self) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        (REPO_ROOT / ".tmp").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=REPO_ROOT / ".tmp")
        root = Path(temp.name)
        for target in validate_readmes.REQUIRED_LINKS:
            path = root / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("target\n", encoding="utf-8")
        names = " · ".join(name for _, name, _ in validate_readmes.LOCALES)
        for locale, current_name, filename in validate_readmes.LOCALES:
            links = []
            for _, name, other_file in validate_readmes.LOCALES:
                links.append(name if name == current_name else f"[{name}]({other_file})")
            body = "# Game Data Analysis Skills\n\n" + " · ".join(links) + "\n\n"
            body += "\n".join(validate_readmes.REQUIRED_LINKS) + "\n"
            if locale == "en":
                body += validate_readmes.CI_BADGE + "\n"
            (root / filename).write_text(body, encoding="utf-8")
        return root, temp

    def test_valid_readme_set_passes(self) -> None:
        root, temp = self.make_tree()
        try:
            self.assertEqual(validate_readmes.validate(root)["status"], "pass")
        finally:
            temp.cleanup()

    def test_missing_locale_link_blocks(self) -> None:
        root, temp = self.make_tree()
        try:
            path = root / "README.es.md"
            path.write_text(path.read_text(encoding="utf-8").replace("[日本語](README.ja.md)", "日本語"), encoding="utf-8")
            findings = validate_readmes.validate(root)["findings"]
            self.assertTrue(any(item["id"] == "language_link_missing" for item in findings))
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
