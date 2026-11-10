"""The 3-tier decision engine and application-level assessment seam.

This module owns the only deliberate business-logic gap in the scaffold:
``DecisionEngine.evaluate``. Everything around it -- strict normalization,
trusted registry resolution, policy loading, and package fingerprinting -- is
wired and deterministic.

When Slice 2 is implemented, ``evaluate`` must enforce this precedence:

    1. BLOCK
       - policy_id mismatch, policy not effective, or unknown claim type
       - any required document missing or unverified
    2. HUMAN_REVIEW
       - required verified fact missing
       - a verified fact matches a review trigger
       - incident_date falls inside the coverage waiting period
    3. PASS
       - every requirement satisfied and no review trigger matched

The engine is a pure function of the normalized claim, validated policy, and
resolved registry records. It must never read raw model prose, wall-clock
time, or the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..domain.claim import NormalizedClaim
from ..domain.policy import Policy
from .audit import compute_package_hash
from .registry import EvidenceRecord, EvidenceRegistry, FactRecord

__all__ = [
    "Decision",
    "DecisionEngine",
    "DecisionResult",
    "Evaluation",
    "assess_claim",
]


class Decision(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    decision: Decision
    code: str
    message: str
    reasons: tuple[str, ...] = ()


class DecisionEngine:
    """Pure 3-tier evaluator over already-resolved trusted records."""

    def evaluate(
        self,
        claim: NormalizedClaim,
        policy: Policy,
        evidence: tuple[EvidenceRecord, ...] | list[EvidenceRecord],
        facts: tuple[FactRecord, ...] | list[FactRecord],
    ) -> DecisionResult:
        """Return PASS, BLOCK, or HUMAN_REVIEW for one claim package.

        TODO(slice-2): implement the checks documented in the module
        docstring. Keep this method free of SQLite, filesystem, network, and
        clock access so it remains exhaustively testable.
        """

        raise NotImplementedError(
            "ClaimGate decision engine is scaffolded but not implemented yet; "
            "the gate fails closed until Slice 2 is complete."
        )


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The complete deterministic assessment of a submitted package."""

    claim: NormalizedClaim
    policy: Policy
    evidence: tuple[EvidenceRecord, ...]
    facts: tuple[FactRecord, ...]
    result: DecisionResult
    package_hash: str


def assess_claim(
    claim: NormalizedClaim,
    policy: Policy,
    registry: EvidenceRegistry,
    *,
    engine: DecisionEngine | None = None,
) -> Evaluation:
    """Resolve trusted records, evaluate policy, and fingerprint the package.

    Registry violations (missing, forged, cross-claim, or unverified IDs) are
    raised before the engine runs. Callers on the enforcement path must turn
    those exceptions into a hard block.
    """

    evidence = registry.resolve_evidence_ids(claim.claim_id, claim.evidence_ids)
    facts = registry.resolve_fact_ids(claim.claim_id, claim.incident_fact_ids)
    resolved_engine = engine or DecisionEngine()
    result = resolved_engine.evaluate(claim, policy, evidence, facts)
    package_hash = compute_package_hash(
        claim=claim,
        policy_hash=policy.source_hash,
        evidence=evidence,
        facts=facts,
    )
    return Evaluation(
        claim=claim,
        policy=policy,
        evidence=evidence,
        facts=facts,
        result=result,
        package_hash=package_hash,
    )
