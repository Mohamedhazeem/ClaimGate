"""The 3-tier decision engine and application-level assessment seam.

``DecisionEngine.evaluate`` is a thin, pure adapter over
``claim_gate.domain.policy.evaluate_claim``. The rules live in the domain
layer so they can be unit-tested without a database; this module only
composes them with the registry and the package fingerprint.

The enforced precedence is:

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
resolved registry records. It never reads raw model prose, wall-clock time,
or the database.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.claim import NormalizedClaim
from ..domain.policy import Decision, DecisionResult, Policy, evaluate_claim
from .audit import compute_package_hash
from .registry import EvidenceRecord, EvidenceRegistry, FactRecord

__all__ = [
    "Decision",
    "DecisionEngine",
    "DecisionResult",
    "Evaluation",
    "assess_claim",
]


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

        The method holds no SQLite, filesystem, network, or clock access, so
        it stays exhaustively testable. All policy rules are implemented by
        ``claim_gate.domain.policy.evaluate_claim``; this method only adapts
        the registry records to the domain layer.
        """

        return evaluate_claim(claim, policy, evidence, facts)


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

    evidence = registry.resolve_evidence_ids(claim.evidence_ids, claim.claim_id)
    facts = registry.resolve_fact_ids(claim.incident_fact_ids, claim.claim_id)
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
