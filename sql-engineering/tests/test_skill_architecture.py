import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "sql-engineering"


class SkillArchitectureTests(unittest.TestCase):
    def test_skill_entrypoint_stays_within_context_budget(self) -> None:
        lines = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 180)

    def test_global_core_rules_do_not_embed_project_business_defaults(self) -> None:
        core = (SKILL_ROOT / "references" / "core-rules.md").read_text(encoding="utf-8")
        forbidden = [
            "DEMO_AB_TEST",
            "demo_warehouse.demo_user_tags",
            "2026-05-14",
            "A包",
            "GameMode = 23",
        ]
        for value in forbidden:
            self.assertNotIn(value, core)

    def test_capability_registry_is_the_single_route_source(self) -> None:
        registry = json.loads(
            (SKILL_ROOT / "references" / "capabilities.json").read_text(encoding="utf-8")
        )
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(registry["schema_version"], "sql_capability_registry_v1")
        self.assertIn("capabilities.json", skill)
        self.assertNotIn("## Route Selection", skill)

    def test_function_gates_do_not_define_local_route_allowlists(self) -> None:
        scripts_dir = SKILL_ROOT / "scripts"
        gated_scripts = []
        for path in scripts_dir.glob("*.py"):
            if path.name == "function_gate.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "require_user_function_selection(" not in text:
                continue
            gated_scripts.append(path.name)
            self.assertTrue(
                "command_function_ids" in text or "command_routes" in text,
                f"{path.name} must resolve function ids through capabilities.json",
            )
            self.assertNotRegex(text, r"(?m)^[A-Z][A-Z0-9_]*FUNCTION_IDS\s*=")
            self.assertNotRegex(text, r"allowed_ids\s*=\s*\{")
        self.assertTrue(gated_scripts)

    def test_obsolete_mixed_repository_entrypoint_is_removed(self) -> None:
        self.assertFalse((SKILL_ROOT / "scripts" / "asset_review.py").exists())
        project_script = (SKILL_ROOT / "scripts" / "sql_project.py").read_text(encoding="utf-8")
        self.assertNotIn('sub.add_parser("discard-sql"', project_script)
        self.assertNotIn('sub.add_parser("search-discarded-sql"', project_script)


if __name__ == "__main__":
    unittest.main()
