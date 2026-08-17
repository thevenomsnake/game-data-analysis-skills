import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sql_identifier_policy as policy  # noqa: E402


CONFIG = {
    "identifier_policy": {
        "quote_style": "backtick",
        "case_sensitive_fields": ["dtEventTime"],
    }
}


class SqlIdentifierPolicyTests(unittest.TestCase):
    def test_quote_preserves_comments_literals_and_existing_quotes(self) -> None:
        sql = """-- dtEventTime
SELECT t.dtEventTime, t.`dtEventTime`, 'dtEventTime' AS sample
FROM source t
"""

        quoted = policy.quote_required_identifiers(sql, CONFIG)

        self.assertIn("-- dtEventTime", quoted)
        self.assertIn("t.`dtEventTime`, t.`dtEventTime`", quoted)
        self.assertIn("'dtEventTime'", quoted)
        self.assertEqual(policy.policy_findings(quoted, CONFIG), [])

    def test_wrong_case_is_reported_not_silently_rewritten(self) -> None:
        sql = "SELECT t.dteventtime FROM source t"

        self.assertEqual(policy.quote_required_identifiers(sql, CONFIG), sql)
        findings = policy.policy_findings(sql, CONFIG)

        self.assertEqual([item["code"] for item in findings], ["case_mismatched_identifier"])


if __name__ == "__main__":
    unittest.main()
