from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.domain.policy import (  # noqa: E402
    PolicyValidationError,
    load_policy,
    parse_policy,
)

POLICY_PATH = ROOT / "policies" / "auto_policy.json"


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    def test_shipped_policy_loads(self) -> None:
        policy = load_policy(POLICY_PATH)
        self.assertEqual(policy.policy_id, "AUTO-001")
        self.assertEqual(len(policy.source_hash), 64)
        self.assertTrue(policy.is_effective_for(date(2026, 1, 1)))
        self.assertTrue(policy.is_effective_for(date(2026, 12, 31)))
        self.assertFalse(policy.is_effective_for(date(2027, 1, 1)))

        coverage = policy.coverage_for("collision")
        self.assertIsNotNone(coverage)
        assert coverage is not None
        self.assertTrue(coverage.enabled)
        self.assertEqual(
            coverage.required_documents,
            ("police_report", "repair_estimate", "photo_evidence"),
        )
        self.assertEqual(coverage.required_verified_facts, ("activity",))
        self.assertTrue(coverage.review_triggers[0].matches("activity", "racing"))
        self.assertFalse(coverage.review_triggers[0].matches("activity", "commuting"))
        self.assertIsNotNone(coverage.waiting_period)
        assert coverage.waiting_period is not None
        self.assertEqual(
            coverage.waiting_period.coverage_available_on, date(2026, 1, 31)
        )

    def test_policy_hash_is_stable_and_content_addressed(self) -> None:
        first = load_policy(POLICY_PATH)
        second = load_policy(POLICY_PATH)
        self.assertEqual(first.source_hash, second.source_hash)

        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["required_documents"].append("photos")
        third = parse_policy(changed)
        self.assertNotEqual(first.source_hash, third.source_hash)

    def test_unknown_keys_are_rejected(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["unexpected"] = True
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["required_document"] = ["typo"]
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

    def test_unknown_waiting_period_key_is_rejected(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["waiting_period"]["days"] = 30
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

    def test_malformed_dates_are_rejected(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["policy_term"]["effective_from"] = "2026-1-1"
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["waiting_period"][
            "coverage_available_on"
        ] = "31/01/2026"
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

    def test_term_must_be_ordered(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["policy_term"]["effective_from"] = "2026-12-31"
        changed["policy_term"]["effective_through"] = "2026-01-01"
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

    def test_public_view_is_normalized(self) -> None:
        policy = load_policy(POLICY_PATH)
        public = policy.to_public_dict()
        self.assertEqual(public["policy_id"], "AUTO-001")
        self.assertEqual(
            public["coverages"]["collision"]["required_documents"],
            ["photo_evidence", "police_report", "repair_estimate"],
        )
        self.assertTrue(public["coverages"]["collision"]["enabled"])
        self.assertEqual(
            public["coverages"]["collision"]["waiting_period"][
                "coverage_available_on"
            ],
            "2026-01-31",
        )

    def test_enabled_flag_is_parsed_and_validated(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["enabled"] = False
        policy = parse_policy(changed)
        coverage = policy.coverage_for("collision")
        assert coverage is not None
        self.assertFalse(coverage.enabled)

        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["enabled"] = "yes"
        with self.assertRaises(PolicyValidationError):
            parse_policy(changed)

    def test_requirement_order_follows_the_policy_document(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["coverages"]["collision"]["required_documents"] = [
            "repair_estimate",
            "police_report",
        ]
        policy = parse_policy(changed)
        coverage = policy.coverage_for("collision")
        assert coverage is not None
        self.assertEqual(
            coverage.required_documents, ("repair_estimate", "police_report")
        )


if __name__ == "__main__":
    unittest.main()
