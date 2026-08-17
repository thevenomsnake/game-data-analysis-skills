import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "sql-engineering" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sql_facts import (  # noqa: E402
    build_sql_fact_bundle,
    execution_fingerprint,
    logic_fingerprint,
    sql_side_privacy_transforms,
)
import sql_facts  # noqa: E402
import sql_formalize  # noqa: E402
import performance_preflight  # noqa: E402
import sql_project  # noqa: E402


def sample_sql(*, start: str = "2026-07-01", end: str = "2026-07-09", zone: int = 10001, mode: int = 6) -> str:
    return f"""WITH params AS (
    SELECT
        '{start}' AS pt_start,
        '{end}' AS pt_end,
        {zone} AS zone_id,
        {mode} AS game_mode
),
base AS (
    SELECT e.vOpenID
    FROM demo_log.demo_dsl_battleloginout_fht0 e
    CROSS JOIN params p
    WHERE e.dtEventDate >= p.pt_start
      AND e.dtEventDate <= p.pt_end
      AND e.iZoneAreaID = p.zone_id
      AND e.GameMode = p.game_mode
)
SELECT COUNT(DISTINCT vOpenID) AS `玩家数`
FROM base;
"""


def subject_identity_config() -> dict:
    return {
        "subject_identity_policy": {
            "contract_version": "subject_identity_policy_v1",
            "business_subject": "player",
            "default_key": "vOpenID",
            "key_definitions": [
                {
                    "key": "vOpenID",
                    "namespace": "vOpenID",
                    "unique_per_person": True,
                    "uniqueness_scope": "test_project",
                },
                {
                    "key": "RoleID",
                    "namespace": "RoleID",
                    "unique_per_person": True,
                    "uniqueness_scope": "test_project",
                },
            ],
            "namespace_relationship": {
                "relation": "same_person_distinct_namespaces",
                "direct_comparison_allowed": False,
                "coalesce_allowed": False,
            },
            "native_role_fields": [
                {
                    "role": "player",
                    "field": "vOpenID",
                    "key": "vOpenID",
                    "canonical_alias": "player_id",
                    "metric_terms": [],
                },
                {
                    "role": "player",
                    "field": "RoleID",
                    "key": "RoleID",
                    "canonical_alias": "player_id",
                    "metric_terms": [],
                },
                {
                    "role": "killer",
                    "field": "DamageSourceVRoleID",
                    "key": "RoleID",
                    "canonical_alias": "killer_player_id",
                    "metric_terms": ["击杀人数", "killer"],
                },
                {
                    "role": "victim",
                    "field": "DamageTargetVRoleID",
                    "key": "RoleID",
                    "canonical_alias": "victim_player_id",
                    "metric_terms": ["死亡人数", "victim"],
                },
            ],
            "selection_policy": {
                "prefer_default_when_equal_cost": True,
                "prefer_native_role_when_avoids_bridge": True,
                "forbid_bridge_only_for_default_key": True,
            },
        }
    }


def write_subject_identity_config(root: Path) -> None:
    (root / "project_config.json").write_text(
        json.dumps(subject_identity_config(), ensure_ascii=False),
        encoding="utf-8",
    )


class SqlFactBundleTests(unittest.TestCase):
    def test_sql_project_reexports_the_shared_fact_analyzer(self) -> None:
        self.assertIs(sql_project.analyze_sql_file, sql_facts.analyze_sql_file)
        self.assertIs(sql_project.extract_tables, sql_facts.extract_tables)
        self.assertIs(sql_project.extract_fields, sql_facts.extract_fields)

    def test_time_parameter_refresh_changes_execution_but_not_logic_fingerprint(self) -> None:
        first = sample_sql()
        refreshed = sample_sql(start="2026-07-02", end="2026-07-10")

        self.assertNotEqual(execution_fingerprint(first), execution_fingerprint(refreshed))
        self.assertEqual(logic_fingerprint(first), logic_fingerprint(refreshed))

    def test_business_parameter_changes_invalidate_logic_fingerprint(self) -> None:
        original = sample_sql()

        self.assertNotEqual(logic_fingerprint(original), logic_fingerprint(sample_sql(zone=10002)))
        self.assertNotEqual(logic_fingerprint(original), logic_fingerprint(sample_sql(mode=7)))

    def test_string_literal_case_and_spacing_remain_logic_significant(self) -> None:
        original = sample_sql().replace("AND e.GameMode = p.game_mode", "AND e.GameMode = p.game_mode\n      AND e.Channel = 'Alpha  One'")
        case_changed = original.replace("'Alpha  One'", "'alpha  One'")
        spacing_changed = original.replace("'Alpha  One'", "'Alpha One'")

        self.assertNotEqual(logic_fingerprint(original), logic_fingerprint(case_changed))
        self.assertNotEqual(logic_fingerprint(original), logic_fingerprint(spacing_changed))

    def test_sql_code_case_does_not_change_logic_fingerprint(self) -> None:
        original = sample_sql()
        lowercased = original.lower()

        self.assertEqual(logic_fingerprint(original), logic_fingerprint(lowercased))

    def test_time_parser_format_is_not_erased_with_time_value(self) -> None:
        original = sample_sql().replace("'2026-07-01' AS pt_start", "str_to_date('2026-07-01', '%Y-%m-%d') AS pt_start")
        refreshed = original.replace("'2026-07-01'", "'2026-07-02'")
        format_changed = original.replace("'%Y-%m-%d'", "'%Y%m%d'")

        self.assertEqual(logic_fingerprint(original), logic_fingerprint(refreshed))
        self.assertNotEqual(logic_fingerprint(original), logic_fingerprint(format_changed))

    def test_formalize_reuses_semantics_but_not_exact_sql_gates_after_date_refresh(self) -> None:
        original = sample_sql()
        refreshed = sample_sql(start="2026-07-02", end="2026-07-10")
        config: dict = {}
        seed = {
            "normalized_sql_fingerprint": execution_fingerprint(original),
            "logic_fingerprint": logic_fingerprint(original),
            "project_config_fingerprint": sql_formalize.config_fingerprint(config),
            "analysis": {"metrics": ["玩家数"], "dimensions": []},
            "performance_level": {"performance_fingerprint": "perf-original"},
        }

        analysis = sql_formalize.reusable_logic_seed_dict(
            seed,
            "analysis",
            raw_sql=refreshed,
            normalized_sql=refreshed,
        )
        performance = sql_formalize.reusable_performance_level(
            seed,
            raw_sql=refreshed,
            normalized_sql=refreshed,
            config=config,
        )

        self.assertEqual(analysis, seed["analysis"])
        self.assertIsNone(performance)

    def test_bundle_uses_physical_sources_and_resolves_param_filters(self) -> None:
        bundle = build_sql_fact_bundle(sample_sql())

        self.assertEqual(bundle["schema_version"], "sql_fact_bundle_v3")
        self.assertEqual(bundle["fingerprint_version"], "sql_fingerprint_v2")
        self.assertEqual(bundle["source_tables"], ["demo_log.demo_dsl_battleloginout_fht0"])
        self.assertEqual(bundle["cte_names"], ["params", "base"])
        self.assertEqual(bundle["final_fields"], ["玩家数"])
        self.assertIn("iZoneAreaID = 10001", [item["condition"] for item in bundle["filters"]])
        self.assertIn("GameMode = 6", [item["condition"] for item in bundle["filters"]])
        self.assertEqual(bundle["performance"]["cte_count"], 2)
        self.assertEqual(bundle["metrics"][0]["dedup_key"], "vOpenID")
        self.assertEqual(bundle["subject_identity"]["default_key"], "vOpenID")

    def test_native_killer_and_victim_role_ids_avoid_identity_bridge(self) -> None:
        sql = """
        SELECT
            COUNT(DISTINCT d.DamageSourceVRoleID) AS `击杀人数`,
            COUNT(DISTINCT d.DamageTargetVRoleID) AS `死亡人数`
        FROM demo_log.demo_dsl_damage_fht0 d
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_subject_identity_config(root)
            bundle = build_sql_fact_bundle(sql, root=root)
            outer_alias_binding = build_sql_fact_bundle(
                """
                WITH base AS (
                    SELECT DamageTargetVRoleID, COUNT(1) AS event_cnt
                    FROM demo_log.demo_dsl_damage_fht0
                    GROUP BY DamageTargetVRoleID
                )
                SELECT COUNT(1) AS `被击杀玩家人数` FROM base
                """,
                root=root,
            )

        metrics = {item["name"]: item for item in bundle["metrics"]}
        entities = {
            item["subject_ref"]: item
            for item in bundle["subject_identity"]["subject_entities"]
        }
        self.assertEqual(metrics["击杀人数"]["dedup_key"], "DamageSourceVRoleID")
        self.assertEqual(metrics["击杀人数"]["subject_ref"], "killer")
        self.assertEqual(metrics["死亡人数"]["dedup_key"], "DamageTargetVRoleID")
        self.assertEqual(metrics["死亡人数"]["subject_ref"], "victim")
        self.assertEqual(entities["killer"]["key_namespace"], "RoleID")
        self.assertEqual(entities["victim"]["key_namespace"], "RoleID")
        self.assertFalse(bundle["subject_identity"]["identity_bridge"]["detected"])
        self.assertEqual(bundle["subject_identity"]["complexity_audit"]["status"], "ok")

        self.assertEqual(outer_alias_binding["metrics"][0]["subject_ref"], "victim")

    def test_vopenid_conversion_join_records_native_role_optimization(self) -> None:
        sql = """
        WITH damage_base AS (
            SELECT d.DamageSourceVRoleID
            FROM demo_log.demo_dsl_damage_fht0 d
        ),
        identity_bridge AS (
            SELECT p.RoleID, p.vOpenID
            FROM demo_log.demo_dsl_playerlogin_fht0 p
        )
        SELECT COUNT(DISTINCT i.vOpenID) AS `击杀人数`
        FROM damage_base d
        JOIN identity_bridge i
          ON d.DamageSourceVRoleID = i.RoleID
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_subject_identity_config(root)
            bundle = build_sql_fact_bundle(sql, root=root)

        metric = bundle["metrics"][0]
        identity = bundle["subject_identity"]
        self.assertEqual(metric["dedup_key"], "vOpenID")
        self.assertEqual(metric["lower_complexity_alternative"]["dedup_key"], "DamageSourceVRoleID")
        self.assertTrue(identity["identity_bridge"]["detected"])
        self.assertEqual(identity["identity_bridge"]["status"], "business_reason_required")
        self.assertEqual(identity["complexity_audit"]["status"], "optimization_available")

    def test_params_join_does_not_become_an_identity_bridge(self) -> None:
        sql = """
        WITH params AS (SELECT '2026-07-09' AS pt_start),
        damage_base AS (
            SELECT d.DamageSourceVRoleID, d.vOpenID
            FROM demo_log.demo_dsl_damage_fht0 d
            JOIN params p ON 1 = 1
        )
        SELECT COUNT(DISTINCT DamageSourceVRoleID) AS `击杀人数`
        FROM damage_base
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_subject_identity_config(root)
            bundle = build_sql_fact_bundle(sql, root=root)

        identity = bundle["subject_identity"]
        self.assertFalse(identity["identity_bridge"]["detected"])
        self.assertEqual(identity["identity_bridge"]["status"], "native_event_key_selected")
        self.assertEqual(identity["complexity_audit"]["status"], "ok")

    def test_sql_side_deidentification_is_blocked_but_raw_business_id_is_not_transformed(self) -> None:
        raw_detail = sample_sql().replace(
            "COUNT(DISTINCT vOpenID) AS `玩家数`",
            "vOpenID AS `玩家ID`",
        )
        hashed_detail = raw_detail.replace("vOpenID AS `玩家ID`", "MD5(vOpenID) AS `玩家ID`")

        raw_bundle = build_sql_fact_bundle(raw_detail)
        hashed_bundle = build_sql_fact_bundle(hashed_detail)
        contract = sql_project.project_execution_contract_check(hashed_detail, {})

        self.assertTrue(raw_bundle["privacy"]["final_raw_identifier_exposed"])
        self.assertEqual(raw_bundle["privacy"]["sql_side_privacy_transforms"], [])
        self.assertEqual(raw_bundle["privacy"]["privacy_handling_owner"], "DA")
        self.assertEqual(sql_side_privacy_transforms(hashed_detail)[0]["function"], "md5")
        self.assertEqual(sql_side_privacy_transforms("SELECT 'md5(vOpenID)' AS example_text"), [])
        self.assertEqual(hashed_bundle["privacy"]["sql_side_privacy_transforms"][0]["function"], "md5")
        self.assertEqual(contract["status"], "conflict")
        self.assertIn(
            "sql_side_privacy_transform",
            {item["type"] for item in contract["blockers"]},
        )

    def test_project_business_scope_owns_default_zone_and_identifier_boundary(self) -> None:
        config = {
            "business_scope": {
                "contract_version": "project_business_scope_v1",
                "default_zone": {
                    "field": "iZoneAreaID",
                    "value": 10001,
                    "parameter_alias": "zone_id",
                    "required_when_available": True,
                },
                "zone_identifier": {
                    "business_field": "iZoneAreaID",
                    "non_equivalent_fields": ["GameSvrId"],
                },
            }
        }
        wrong_zone = """
        WITH params AS (SELECT 20001 AS zone_id)
        SELECT COUNT(1) FROM demo_log.demo_dsl_playerlogin_fht0 p
        JOIN params x ON 1 = 1 WHERE p.iZoneAreaID = x.zone_id
        """
        wrong_identifier = "SELECT GameSvrId AS zone_id FROM some_table"
        right_zone = """
        WITH params AS (SELECT 10001 AS zone_id)
        SELECT COUNT(1) FROM demo_log.demo_dsl_playerlogin_fht0 p
        JOIN params x ON 1 = 1 WHERE p.iZoneAreaID = x.zone_id
        """
        self.assertEqual(
            sql_project.project_execution_contract_check(wrong_zone, config)["status"],
            "conflict",
        )
        self.assertEqual(
            sql_project.project_execution_contract_check(wrong_identifier, config)["status"],
            "conflict",
        )
        self.assertNotEqual(
            sql_project.project_execution_contract_check(right_zone, config)["status"],
            "conflict",
        )

    def test_performance_preflight_consumes_matching_fact_bundle(self) -> None:
        sql = sample_sql()
        bundle = build_sql_fact_bundle(sql)
        result = performance_preflight.analyze_performance(sql=sql, sql_facts=bundle, project_config={})

        self.assertEqual(result["facts"]["sql_fact_source"], "provided_sql_fact_bundle")
        with self.assertRaisesRegex(ValueError, "stale SqlFactBundle"):
            performance_preflight.analyze_performance(
                sql=sample_sql(mode=7),
                sql_facts=bundle,
                project_config={},
            )

    def test_external_source_contract_is_project_data_not_global_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sources").mkdir()
            contract = {
                "schema_version": "external_table_contract_v1",
                "table": "partner.user_cohort_di",
                "source_type": "partner_table",
                "availability_status": "partner_query_only",
                "description": "Authoritative user cohort table.",
                "business_scope": ["new_user"],
                "columns": [{"name": "cohort_date", "meaning": "Cohort date"}],
            }
            (root / "sources" / "user_cohort.schema.json").write_text(
                json.dumps(contract), encoding="utf-8"
            )

            bundle = build_sql_fact_bundle(
                "SELECT COUNT(*) AS user_cnt FROM partner.user_cohort_di",
                root=root,
            )

        self.assertEqual(bundle["external_sources"][0]["table"], "partner.user_cohort_di")
        self.assertEqual(bundle["external_sources"][0]["source_contract"], "sources/user_cohort.schema.json")


if __name__ == "__main__":
    unittest.main()
