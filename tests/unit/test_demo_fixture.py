"""Slice 4 tests: the demo fixture must never drift from the runtime.

The demo is only credible if what it resets is exactly what the plugin
uses. These tests pin that relationship:

* the canonical policy template in ``demo/reset.py`` is byte-equivalent to
  the shipped ``policies/auto_policy.json``;
* ``demo/reset.py`` really drops, recreates, and re-seeds every ClaimGate
  table;
* the reset fixture leaves the registries in a state where every seeded
  reference resolves and the demo scenarios produce their expected
  decisions.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, "/home/hazeem/.hermes/hermes-agent")


def _load_demo_module():
    """Import ``demo/reset.py`` without making ``demo`` a package."""

    spec = importlib.util.spec_from_file_location(
        "claimgate_demo_reset", ROOT / "demo" / "reset.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_home = os.environ.get("HERMES_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._temp.name
        self.reset = _load_demo_module()

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self._temp.cleanup()

    def test_policy_template_matches_the_shipped_policy(self) -> None:
        shipped = json.loads(
            (ROOT / "policies" / "auto_policy.json").read_text(encoding="utf-8")
        )
        self.assertEqual(self.reset.POLICY_TEMPLATE, shipped)

    def test_reset_restores_the_policy_file(self) -> None:
        policy_path = ROOT / "policies" / "auto_policy.json"
        original = policy_path.read_text(encoding="utf-8")
        try:
            policy_path.write_text("{ not json", encoding="utf-8")
            policy_hash = self.reset.reset_policy(policy_path)
            self.assertEqual(len(policy_hash), 64)
            restored = json.loads(policy_path.read_text(encoding="utf-8"))
            self.assertEqual(restored, self.reset.POLICY_TEMPLATE)
        finally:
            policy_path.write_text(original, encoding="utf-8")

    def test_reset_drops_and_reseeds_every_table(self) -> None:
        from claim_gate.services import registry

        conn = registry.connect()
        conn.execute(
            "INSERT INTO audit_log (tool_call_id, claim_id, decision) "
            "VALUES ('stale', 'CLAIM-101', 'PASS')"
        )
        conn.execute(
            "INSERT INTO claim_ledger (claim_id, package_hash) "
            "VALUES ('CLAIM-101', ?)",
            ("a" * 64,),
        )
        conn.execute(
            "INSERT INTO human_review_queue (claim_id, package_hash) "
            "VALUES ('CLAIM-101', ?)",
            ("a" * 64,),
        )
        conn.commit()
        conn.close()

        self.reset.reset_database()

        conn = registry.connect()
        try:
            for table in ("audit_log", "claim_ledger", "human_review_queue"):
                count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0, f"{table} was not cleared")

            evidence = {
                row[0]
                for row in conn.execute("SELECT evidence_id FROM evidence_registry")
            }
            facts = {
                row[0]
                for row in conn.execute("SELECT fact_id FROM incident_fact_registry")
            }
            self.assertLessEqual(
                {"EV-001", "EV-002", "EV-003", "EV-004"}, evidence
            )
            self.assertLessEqual({"FACT-001", "FACT-002"}, facts)
        finally:
            conn.close()

    def test_reset_is_idempotent(self) -> None:
        self.reset.reset_database()
        self.reset.reset_database()

        from claim_gate.services import registry

        conn = registry.connect()
        try:
            evidence = conn.execute(
                "SELECT count(*) FROM evidence_registry"
            ).fetchone()[0]
            facts = conn.execute(
                "SELECT count(*) FROM incident_fact_registry"
            ).fetchone()[0]
            self.assertEqual(evidence, len(registry.SEED_EVIDENCE))
            self.assertEqual(facts, len(registry.SEED_FACTS))
        finally:
            conn.close()

    def test_demo_fixture_resolves_and_blocks_as_documented(self) -> None:
        from claim_gate.hooks.execution_gate import pre_tool_call

        self.reset.reset_database()

        def claim(evidence_ids, incident_date, fact_ids):
            return {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": incident_date,
                "claim_type": "collision",
                "evidence_ids": evidence_ids,
                "incident_fact_ids": fact_ids,
            }

        # Missing repair_estimate -> BLOCK.
        self.assertEqual(
            pre_tool_call(
                "submit_claim_package",
                claim(["EV-001", "EV-002"], "2026-11-04", ["FACT-002"]),
            ),
            {
                "action": "block",
                "message": "Missing required document: repair_estimate",
            },
        )

        # An invented ID -> BLOCK.
        self.assertEqual(
            pre_tool_call(
                "submit_claim_package",
                claim(
                    ["EV-001", "EV-002", "EV-003", "EV-999"],
                    "2026-11-05",
                    ["FACT-002"],
                ),
            ),
            {"action": "block", "message": "ClaimGate: Forged ID"},
        )

        # Inside the waiting period -> HUMAN_REVIEW.
        self.assertEqual(
            pre_tool_call(
                "submit_claim_package",
                claim(["EV-001", "EV-002", "EV-003"], "2026-01-10", ["FACT-002"]),
            ),
            {"action": "block", "message": "ClaimGate: Pending Human Review"},
        )

        # A complete package -> PASS.
        self.assertIsNone(
            pre_tool_call(
                "submit_claim_package",
                claim(["EV-001", "EV-002", "EV-003"], "2026-11-06", ["FACT-002"]),
            )
        )


if __name__ == "__main__":
    unittest.main()
