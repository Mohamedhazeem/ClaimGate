"""Slice 2 unit tests for the pure 3-tier policy evaluation.

These tests exercise ``claim_gate.domain.policy.evaluate_claim`` through
``DecisionEngine`` with the real shipped policy and the real registry record
types. No database, no clock, and no model prose is involved.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.domain.claim import normalize_claim  # noqa: E402
from claim_gate.domain.policy import Decision, load_policy  # noqa: E402
from claim_gate.services.engine import DecisionEngine  # noqa: E402
from claim_gate.services.registry import EvidenceRecord, FactRecord  # noqa: E402

POLICY_PATH = ROOT / "policies" / "auto_policy.json"


def evidence(evidence_id: str, document_type: str, status: str = "verified") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="CLAIM-101",
        document_type=document_type,
        status=status,
        verified_by="system" if status == "verified" else None,
    )


def fact(
    fact_id: str,
    fact_type: str,
    fact_value: str,
    status: str = "verified",
) -> FactRecord:
    return FactRecord(
        fact_id=fact_id,
        claim_id="CLAIM-101",
        fact_type=fact_type,
        fact_value=fact_value,
        status=status,
    )


POLICE_REPORT = evidence("EV-001", "police_report")
REPAIR_ESTIMATE = evidence("EV-003", "repair_estimate")
PHOTO_EVIDENCE = evidence("EV-002", "photo_evidence")

RACING = fact("FACT-001", "activity", "racing")
COMMUTING = fact("FACT-002", "activity", "commuting")


def claim(
    evidence_ids: list[str],
    *,
    incident_date: str = "2026-11-06",
    fact_ids: list[str] | None = None,
    claim_id: str = "CLAIM-101",
    policy_id: str = "AUTO-001",
    claim_type: str = "collision",
):
    return normalize_claim(
        {
            "claim_id": claim_id,
            "policy_id": policy_id,
            "incident_date": incident_date,
            "claim_type": claim_type,
            "evidence_ids": evidence_ids,
            "incident_fact_ids": fact_ids if fact_ids is not None else [],
        }
    )


class DecisionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(POLICY_PATH)
        self.engine = DecisionEngine()

    def evaluate(self, normalized_claim, evidence=(), facts=()):
        return self.engine.evaluate(normalized_claim, self.policy, list(evidence), list(facts))

    # -- BLOCK ----------------------------------------------------------

    def test_missing_repair_estimate_blocks(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-002"]),
            [POLICE_REPORT, PHOTO_EVIDENCE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "MISSING_DOCUMENT")
        self.assertEqual(result.message, "Missing required document: repair_estimate")

    def test_missing_document_wins_over_missing_fact(self) -> None:
        # BLOCK has precedence over HUMAN_REVIEW: the fact is also missing
        # here, but the document failure is the headline reason.
        result = self.evaluate(claim(["EV-001"]), [POLICE_REPORT], [])
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.message, "Missing required document: repair_estimate")

    def test_unverified_required_document_blocks(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"]),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.PASS)

        pending = evidence("EV-009", "repair_estimate", status="pending")
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"]),
            [POLICE_REPORT, pending, PHOTO_EVIDENCE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "DOCUMENT_NOT_VERIFIED")

    def test_policy_id_mismatch_blocks(self) -> None:
        result = self.evaluate(claim(["EV-001"], policy_id="AUTO-999"))
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "POLICY_MISMATCH")

    def test_policy_term_must_cover_the_incident_date(self) -> None:
        result = self.evaluate(claim(["EV-001"], incident_date="2025-12-31"))
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "POLICY_NOT_EFFECTIVE")

    def test_unknown_claim_type_blocks(self) -> None:
        result = self.evaluate(claim(["EV-001"], claim_type="liability"))
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "NO_COVERAGE")

    # -- HUMAN_REVIEW ---------------------------------------------------

    def test_missing_activity_fact_requires_review(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"]),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [],
        )
        self.assertIs(result.decision, Decision.HUMAN_REVIEW)
        self.assertEqual(result.code, "MISSING_FACT")
        self.assertIn("activity", result.message)

    def test_racing_activity_requires_review(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"], fact_ids=["FACT-001"]),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [RACING],
        )
        self.assertIs(result.decision, Decision.HUMAN_REVIEW)
        self.assertEqual(result.code, "REVIEW_TRIGGER")
        self.assertEqual(result.message, "Review required: activity is racing")

    def test_incident_inside_waiting_period_requires_review(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"], incident_date="2026-01-10"),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.HUMAN_REVIEW)
        self.assertEqual(result.code, "WAITING_PERIOD")
        self.assertEqual(result.message, "Review required: incident within waiting period")

    def test_waiting_period_is_inclusive_of_the_availability_date(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"], incident_date="2026-01-31"),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.PASS)

    def test_waiting_period_uses_incident_date_not_submission_date(self) -> None:
        # An incident inside the waiting period stays inside it no matter
        # when the package is submitted; there is no clock in the engine.
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"], incident_date="2026-01-10"),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [],
        )
        self.assertIs(result.decision, Decision.HUMAN_REVIEW)
        self.assertEqual(result.code, "WAITING_PERIOD")

    def test_unverified_fact_blocks_instead_of_being_trusted(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-003", "EV-002"], fact_ids=["FACT-003"]),
            [POLICE_REPORT, REPAIR_ESTIMATE, PHOTO_EVIDENCE],
            [fact("FACT-003", "activity", "racing", status="pending")],
        )
        self.assertIs(result.decision, Decision.BLOCK)
        self.assertEqual(result.code, "FACT_NOT_VERIFIED")

    # -- PASS -----------------------------------------------------------

    def test_complete_package_passes(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"]),
            [POLICE_REPORT, PHOTO_EVIDENCE, REPAIR_ESTIMATE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.PASS)
        self.assertEqual(result.code, "PASS")

    def test_extra_evidence_does_not_break_a_pass(self) -> None:
        result = self.evaluate(
            claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"]),
            [POLICE_REPORT, PHOTO_EVIDENCE, REPAIR_ESTIMATE],
            [COMMUTING],
        )
        self.assertIs(result.decision, Decision.PASS)

    def test_evaluation_is_deterministic(self) -> None:
        normalized = claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        first = self.evaluate(normalized, [POLICE_REPORT, PHOTO_EVIDENCE, REPAIR_ESTIMATE], [COMMUTING])
        second = self.evaluate(normalized, [REPAIR_ESTIMATE, POLICE_REPORT, PHOTO_EVIDENCE], [COMMUTING])
        self.assertEqual(first, second)


class PolicyTermTests(unittest.TestCase):
    def test_term_covers_only_its_inclusive_window(self) -> None:
        policy = load_policy(POLICY_PATH)
        self.assertTrue(policy.is_effective_for(date(2026, 1, 1)))
        self.assertTrue(policy.is_effective_for(date(2026, 12, 31)))
        self.assertFalse(policy.is_effective_for(date(2025, 12, 31)))
        self.assertFalse(policy.is_effective_for(date(2027, 1, 1)))


if __name__ == "__main__":
    unittest.main()
