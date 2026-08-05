import csv
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "sql-engineering"
READMES = (
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "README.zh-TW.md",
    ROOT / "README.ja.md",
    ROOT / "README.es.md",
    ROOT / "README.ko.md",
)


class PublicOnboardingTests(unittest.TestCase):
    def test_all_language_homepages_expose_project_inputs_and_flow(self) -> None:
        required = (
            "sources/raw/",
            "knowledge/planning/",
            "knowledge/confirmed/",
            "rules/definitions/",
            "project-onboarding.md",
            "references/dialects.md",
            "`environment`",
            "`source`",
            "`knowledge`",
            "`rule`",
            "`status`",
        )
        for readme in READMES:
            content = readme.read_text(encoding="utf-8")
            missing = [token for token in required if token not in content]
            self.assertFalse(missing, f"{readme.name} missing {missing}")

    def test_all_other_homepages_link_to_traditional_chinese(self) -> None:
        for readme in READMES:
            if readme.name != "README.zh-TW.md":
                self.assertIn("README.zh-TW.md", readme.read_text(encoding="utf-8"))

    def test_onboarding_examples_are_parseable_and_fictional(self) -> None:
        examples = SKILL_ROOT / "assets" / "examples"
        telemetry = ET.parse(examples / "telemetry-source.example.xml").getroot()
        self.assertEqual(telemetry.attrib["project"], "example")
        confirmed = json.loads((examples / "confirmed-reference.example.json").read_text(encoding="utf-8"))
        rule = json.loads((examples / "canonical-rule.example.json").read_text(encoding="utf-8"))
        with (examples / "planning-table.example.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(confirmed["mapping_name"], "game_mode_names")
        self.assertEqual(rule["schema_version"], "sql_rule_input_v1")
        self.assertEqual(rule["source_contracts"], ["player-login:v001"])
        self.assertGreaterEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
