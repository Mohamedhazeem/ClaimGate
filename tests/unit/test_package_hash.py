from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.domain.claim import normalize_claim  # noqa: E402
from claim_gate.domain.policy import load_policy  # noqa: E402
from claim_gate.services.audit import compute_package_hash  # noqa: E402
from claim_gate.services.registry import EvidenceRecord, FactRecord  # noqa: E402

POLICY_PATH = ROOT / "policies" / "auto_policy.json"


class PackageHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(POLICY_PATH)
        self.claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-02-01",
                "claim_type": "collision",
                "evidence_ids": ["EV-001", "EV-002"],
                "incident_fact_ids": ["FACT-001"],
            }
        )
        self.evidence = (
            EvidenceRecord("EV-001", "CLAIM-101", "police_report", "verified", "op-1"),
            EvidenceRecord("EV-002", "CLAIM-101", "repair_estimate", "verified", "op-1"),
        )
        self.facts = (
            FactRecord("FACT-001", "CLAIM-101", "activity", "commuting", "verified"),
        )

    def test_hash_is_sha256_and_stable(self) -> None:
        first = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        second = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)  # hexadecimal by construction

    def test_submission_order_does_not_change_hash(self) -> None:
        first = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        second = compute_package_hash(
            self.claim,
            self.policy.source_hash,
            tuple(reversed(self.evidence)),
            tuple(reversed(self.facts)),
        )
        self.assertEqual(first, second)

    def test_evidence_mutation_changes_hash(self) -> None:
        first = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        changed = list(self.evidence)
        changed[0] = EvidenceRecord(
            "EV-001", "CLAIM-101", "police_report", "verified", "different-operator"
        )
        second = compute_package_hash(
            self.claim, self.policy.source_hash, changed, self.facts
        )
        self.assertNotEqual(first, second)

    def test_fact_mutation_changes_hash(self) -> None:
        first = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        changed = (
            FactRecord("FACT-001", "CLAIM-101", "activity", "racing", "verified"),
        )
        second = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, changed
        )
        self.assertNotEqual(first, second)

    def test_policy_hash_change_changes_hash(self) -> None:
        first = compute_package_hash(
            self.claim, self.policy.source_hash, self.evidence, self.facts
        )
        second = compute_package_hash(self.claim, "0" * 64, self.evidence, self.facts)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
