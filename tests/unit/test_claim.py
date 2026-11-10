from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.domain.claim import (  # noqa: E402
    DateFormatError,
    ValidationError,
    normalize_claim,
    parse_iso_date,
)


class ClaimDomainTests(unittest.TestCase):
    def test_parse_exact_iso_date(self) -> None:
        self.assertEqual(parse_iso_date("2026-01-31"), date(2026, 1, 31))

    def test_rejects_non_exact_dates(self) -> None:
        for value in (
            "2026-1-31",
            "20260131",
            " 2026-01-31",
            "2026-01-31 ",
            "2026-01-31T00:00:00",
            "2026-02-30",
            "not-a-date",
            None,
            20260131,
        ):
            with self.subTest(value=value):
                with self.assertRaises(DateFormatError):
                    parse_iso_date(value)

    def test_normalize_sorts_and_deduplicates_ids(self) -> None:
        claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-02-01",
                "claim_type": "Collision",
                "evidence_ids": ["EV-002", "EV-001", "EV-002"],
                "incident_fact_ids": ["FACT-002", "FACT-001"],
            }
        )
        self.assertEqual(claim.evidence_ids, ("EV-001", "EV-002"))
        self.assertEqual(claim.incident_fact_ids, ("FACT-001", "FACT-002"))
        self.assertEqual(claim.claim_type, "collision")

    def test_model_supplied_verification_fields_are_ignored(self) -> None:
        claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-02-01",
                "claim_type": "collision",
                "evidence_ids": ["EV-001"],
                "incident_fact_ids": ["FACT-001"],
                "verified": True,
                "status": "verified",
                "verified_by": "the-model",
                "document_type": "police_report",
                "confidence": 1.0,
            }
        )
        # These fields do not exist on the normalized model at all.
        self.assertFalse(hasattr(claim, "verified"))
        self.assertFalse(hasattr(claim, "status"))
        self.assertFalse(hasattr(claim, "verified_by"))
        self.assertFalse(hasattr(claim, "confidence"))

    def test_waiting_period_uses_incident_date_not_today(self) -> None:
        claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-01-10",
                "claim_type": "collision",
                "evidence_ids": [],
                "incident_fact_ids": [],
            }
        )
        coverage_available_on = date(2026, 1, 31)
        self.assertFalse(claim.is_coverage_available_on(coverage_available_on))
        self.assertEqual(claim.waiting_days_remaining(coverage_available_on), 21)
        self.assertTrue(claim.is_coverage_available_on(date(2026, 1, 10)))
        self.assertEqual(claim.waiting_days_remaining(date(2026, 1, 10)), 0)

    def test_waiting_period_ends_inclusive(self) -> None:
        claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-01-31",
                "claim_type": "collision",
                "evidence_ids": [],
                "incident_fact_ids": [],
            }
        )
        self.assertTrue(claim.is_coverage_available_on(date(2026, 1, 31)))

    def test_requires_array_for_references(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_claim(
                {
                    "claim_id": "CLAIM-101",
                    "policy_id": "AUTO-001",
                    "incident_date": "2026-02-01",
                    "claim_type": "collision",
                    "evidence_ids": "EV-001",
                    "incident_fact_ids": [],
                }
            )

    def test_to_dict_is_json_safe(self) -> None:
        claim = normalize_claim(
            {
                "claim_id": "CLAIM-101",
                "policy_id": "AUTO-001",
                "incident_date": "2026-02-01",
                "claim_type": "collision",
                "evidence_ids": ["EV-001"],
                "incident_fact_ids": ["FACT-001"],
            }
        )
        payload = claim.to_dict()
        self.assertEqual(payload["incident_date"], "2026-02-01")
        self.assertEqual(payload["evidence_ids"], ["EV-001"])


if __name__ == "__main__":
    unittest.main()
