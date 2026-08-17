from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from temporary_rule_override import (  # noqa: E402
    acknowledge_temporary_rule_override,
    build_temporary_rule_override,
    canonical_conflict_signature,
    request_authorizes_temporary_override,
    request_declares_temporary_sql,
    unresolved_temporary_rule_override,
)


def blocker(rule_id: str = "rule-a", expected_log: str = "PlayerLogin") -> dict:
    return {
        "type": "missing_required_log",
        "rule_id": rule_id,
        "concept_key": "new-user",
        "expected_log": expected_log,
        "actual_logs": ["PlayerRegister"],
        "reason": "Saved rule expects PlayerLogin but this query intentionally uses a proxy.",
        "message": "Display text must not affect the signature.",
    }


class TemporaryRuleOverrideTests(unittest.TestCase):
    def test_request_must_explicitly_declare_temporary_scope(self) -> None:
        self.assertTrue(request_declares_temporary_sql("这是临时 SQL，先按我说的查"))
        self.assertTrue(request_declares_temporary_sql("本次先按 PlayerLogin"))
        self.assertFalse(request_declares_temporary_sql("生成正式新增用户查询"))
        self.assertTrue(
            request_authorizes_temporary_override(
                "我确认本次查询以我说的为准，先绕过这个口径冲突"
            )
        )
        self.assertFalse(request_authorizes_temporary_override("继续生成查询"))

    def test_conflict_signature_ignores_order_and_display_message(self) -> None:
        first = [blocker("rule-a"), blocker("rule-b", "PlayerLogout")]
        second = [blocker("rule-b", "PlayerLogout"), blocker("rule-a")]
        second[1]["message"] = "Different prose"
        self.assertEqual(
            canonical_conflict_signature(first),
            canonical_conflict_signature(second),
        )

    def test_same_family_signature_is_acknowledged_without_repeat_notice(self) -> None:
        current = build_temporary_rule_override(
            user_request="这是临时 SQL，按 PlayerLogin",
            blockers=[blocker()],
            acknowledged_at="2026-07-11T01:00:00+00:00",
        )
        first = acknowledge_temporary_rule_override(
            current,
            [],
            acknowledged_at="2026-07-11T01:00:00+00:00",
        )
        repeated = acknowledge_temporary_rule_override(
            current,
            [{"temporary_rule_override": first}],
            acknowledged_at="2026-07-11T02:00:00+00:00",
        )
        self.assertEqual(first["notification_status"], "new")
        self.assertTrue(first["should_notify"])
        self.assertIn("expects PlayerLogin", first["conflict_reasons"][0])
        self.assertEqual(first["follow_up"]["routes"], ["RULES", "SKILL_EVOLUTION"])
        self.assertEqual(repeated["notification_status"], "acknowledged")
        self.assertFalse(repeated["should_notify"])
        self.assertEqual(repeated["first_acknowledged_at"], first["first_acknowledged_at"])

    def test_changed_conflict_requires_new_notice_and_blocks_formalization(self) -> None:
        first = build_temporary_rule_override(
            user_request="临时 SQL",
            blockers=[blocker()],
            acknowledged_at="2026-07-11T01:00:00+00:00",
        )
        changed = build_temporary_rule_override(
            user_request="临时 SQL",
            blockers=[blocker(expected_log="BattleLogInOut")],
            acknowledged_at="2026-07-11T02:00:00+00:00",
        )
        resolved = acknowledge_temporary_rule_override(
            changed,
            [{"temporary_rule_override": first}],
            acknowledged_at="2026-07-11T02:00:00+00:00",
        )
        self.assertEqual(resolved["notification_status"], "new")
        self.assertTrue(resolved["should_notify"])
        self.assertTrue(unresolved_temporary_rule_override(resolved))


if __name__ == "__main__":
    unittest.main()
