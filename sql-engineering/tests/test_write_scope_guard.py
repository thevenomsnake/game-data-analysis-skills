from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import write_scope_guard  # noqa: E402


class WriteScopeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evaluate(self, function_id: str, *paths: Path) -> dict:
        return write_scope_guard.evaluate_write_scope(
            function_selection=function_id,
            paths=list(paths),
            repo_root=self.repo,
        )

    def test_query_cannot_edit_source_or_runtime_skill(self) -> None:
        source = self.repo / "sql-engineering" / "scripts" / "sql_project.py"
        runtime = (
            self.repo
            / "runtime"
            / "codex-home"
            / "skills"
            / "sql-engineering"
            / "SKILL.md"
        )

        result = self.evaluate("QUERY", source, runtime)

        self.assertEqual(result["status"], "block")
        self.assertTrue(all("skill_source" in item["protected_scopes"] for item in result["checks"]))

    def test_only_skill_evolution_can_edit_skill_source(self) -> None:
        source = self.repo / "sql-engineering" / "tests" / "test_rules.py"

        self.assertEqual(self.evaluate("SKILL_EVOLUTION", source)["status"], "pass")
        self.assertEqual(self.evaluate("RULES", source)["status"], "block")

    def test_only_rules_can_edit_canonical_rule_assets(self) -> None:
        canonical = self.repo / "sql-projects" / "DEMO_ANALYTICS" / "rules" / "canonical_rules.json"
        definition = self.repo / "sql-projects" / "DEMO_ANALYTICS" / "rules" / "definitions" / "metric" / "v002.json"
        concepts = self.repo / "sql-projects" / "_rule_review" / "rule_concepts.json"

        self.assertEqual(self.evaluate("RULES", canonical, definition, concepts)["status"], "pass")
        self.assertEqual(self.evaluate("QUERY", canonical)["status"], "block")
        self.assertEqual(self.evaluate("SKILL_EVOLUTION", definition)["status"], "block")
        self.assertEqual(self.evaluate("SKILL_EVOLUTION", concepts)["status"], "block")

    def test_query_workspace_files_are_not_overblocked(self) -> None:
        query = self.repo / "sql-projects" / "DEMO_ANALYTICS" / "query_workspace" / "family" / "v001.sql"

        result = self.evaluate("QUERY", query)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["checks"][0]["protected_scopes"], [])

    def test_only_knowledge_route_can_edit_global_or_project_knowledge(self) -> None:
        contract = self.repo / "knowledge-base" / "contracts" / "items.json"
        binding = self.repo / "sql-projects" / "DEMO_ANALYTICS" / "knowledge" / "bindings.json"

        self.assertEqual(self.evaluate("KNOWLEDGE", contract, binding)["status"], "pass")
        self.assertEqual(self.evaluate("QUERY", contract)["status"], "block")
        self.assertEqual(self.evaluate("RULES", binding)["status"], "block")

    def test_only_planning_or_project_admin_can_edit_planning_assets(self) -> None:
        release = self.repo / "planning-sources" / "RM" / "stages" / "BASE" / "release.json"
        binding = self.repo / "sql-projects" / "DEMO_ANALYTICS" / "planning" / "source_binding.json"

        self.assertEqual(self.evaluate("PLANNING_SOURCE", release, binding)["status"], "pass")
        self.assertEqual(self.evaluate("PROJECT_ADMIN", binding)["status"], "pass")
        self.assertEqual(self.evaluate("QUERY", release)["status"], "block")
        self.assertEqual(self.evaluate("KNOWLEDGE", binding)["status"], "block")


if __name__ == "__main__":
    unittest.main()
