"""Policy schema, strict JSON loading, deterministic hashing, and the pure
3-tier policy evaluation.

The policy is a versioned operator artifact, not model input. It is loaded
from ``policies/auto_policy.json`` (or ``CLAIM_GATE_POLICY``) and validated
before use. Unknown keys are rejected at every level so a typo such as
``required_document`` cannot silently remove a control.

``evaluate_claim`` is the Slice 2 decision function. It is a pure function of
the normalized claim, the validated policy, and the already-resolved trusted
registry rows. It performs no I/O, reads no clock, and never inspects model
prose. The evaluation precedence is fixed:

    1. BLOCK
       - policy id mismatch, policy term does not cover the incident date,
         unknown claim type, or a disabled coverage
       - a required document is missing or not verified
    2. HUMAN_REVIEW
       - the incident date falls inside the coverage waiting period
       - a required verified fact is missing
       - a verified fact matches a review trigger
    3. PASS
       - every requirement is satisfied and no trigger matched

This module stays in the domain layer: it has no database or Hermes imports.
Resolved registry rows are consumed structurally (``Protocol``) so the domain
layer never imports the service layer.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from .claim import NormalizedClaim, ValidationError, parse_iso_date

__all__ = [
    "CoverageRule",
    "Decision",
    "DecisionResult",
    "EvidenceLike",
    "FactLike",
    "Policy",
    "PolicyTerm",
    "PolicyValidationError",
    "ReviewTrigger",
    "WaitingPeriod",
    "canonical_json_bytes",
    "default_policy_path",
    "evaluate_claim",
    "load_policy",
    "parse_policy",
]

DEFAULT_POLICY_ENV = "CLAIM_GATE_POLICY"


class PolicyValidationError(ValidationError):
    """Raised when the policy document or file is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value deterministically as UTF-8 JSON.

    This representation is used for policy and package fingerprints. It is
    independent of whitespace, dictionary insertion order, and the host
    platform's default encoding.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expect_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyValidationError(f"{field} must be a JSON object")
    return value


def _expect_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _policy_date(value: object, *, field: str) -> date:
    try:
        return parse_iso_date(value, field=field)
    except ValidationError as exc:
        raise PolicyValidationError(str(exc)) from exc


def _expect_str_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyValidationError(f"{field} must be a JSON array of strings")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _expect_str(item, field=f"{field}[{index}]")
        if text in seen:
            raise PolicyValidationError(f"{field} contains duplicate value {text!r}")
        seen.add(text)
        result.append(text)
    return tuple(result)


def _reject_unknown(mapping: Mapping[str, Any], *, allowed: set[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise PolicyValidationError(
            f"{field} contains unknown key(s): {', '.join(repr(k) for k in unknown)}"
        )


@dataclass(frozen=True, slots=True)
class ReviewTrigger:
    """A verified fact that forces human review when its value matches."""

    fact_type: str
    values: frozenset[str]

    def matches(self, fact_type: str, fact_value: str) -> bool:
        return self.fact_type == fact_type and fact_value in self.values


@dataclass(frozen=True, slots=True)
class WaitingPeriod:
    """Coverage availability measured from the incident date."""

    coverage_available_on: date

    def incident_is_eligible(self, incident_date: date) -> bool:
        return incident_date >= self.coverage_available_on


class Decision(str, Enum):
    """The three deterministic outcomes of a policy evaluation."""

    PASS = "PASS"
    BLOCK = "BLOCK"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The deterministic outcome of evaluating one claim package.

    ``message`` is the single operator-facing reason for the decision.
    ``reasons`` carries the supporting detail (for example every missing
    document) without changing the headline reason.
    """

    decision: Decision
    code: str
    message: str
    reasons: tuple[str, ...] = ()


class EvidenceLike(Protocol):
    """Structural view of a resolved evidence row.

    The domain layer must not import the service layer, so the evaluator only
    relies on the attributes the registry guarantees. The members are
    declared read-only, which is what the frozen registry records provide.
    """

    @property
    def evidence_id(self) -> str: ...

    @property
    def document_type(self) -> str: ...

    @property
    def status(self) -> str: ...


class FactLike(Protocol):
    """Structural view of a resolved incident-fact row."""

    @property
    def fact_id(self) -> str: ...

    @property
    def fact_type(self) -> str: ...

    @property
    def fact_value(self) -> str: ...

    @property
    def status(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CoverageRule:
    """Deterministic requirements for one claim type.

    ``required_documents`` and ``required_verified_facts`` keep the order in
    which the operator declared them. That order is the reporting priority
    when several requirements are unsatisfied, so the decision a reviewer
    sees never depends on dictionary or set iteration order.
    """

    claim_type: str
    required_documents: tuple[str, ...]
    required_verified_facts: tuple[str, ...]
    review_triggers: tuple[ReviewTrigger, ...]
    waiting_period: WaitingPeriod | None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PolicyTerm:
    """Inclusive effective window for a policy."""

    effective_from: date
    effective_through: date

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_through


@dataclass(frozen=True, slots=True)
class Policy:
    """Immutable, validated policy with a stable content fingerprint."""

    policy_id: str
    term: PolicyTerm
    coverages: Mapping[str, CoverageRule]
    source_hash: str

    def coverage_for(self, claim_type: str) -> CoverageRule | None:
        return self.coverages.get(claim_type)

    def is_effective_for(self, day: date) -> bool:
        return self.term.covers(day)

    def to_public_dict(self) -> dict[str, Any]:
        """Return a normalized, JSON-safe policy view for operator tooling."""

        coverages: dict[str, Any] = {}
        for claim_type in sorted(self.coverages):
            coverage = self.coverages[claim_type]
            item: dict[str, Any] = {
                "enabled": coverage.enabled,
                "required_documents": sorted(coverage.required_documents),
                "required_verified_facts": sorted(coverage.required_verified_facts),
                "review_triggers": [
                    {
                        "fact_type": trigger.fact_type,
                        "values": sorted(trigger.values),
                    }
                    for trigger in sorted(
                        coverage.review_triggers,
                        key=lambda t: (t.fact_type, sorted(t.values)),
                    )
                ],
            }
            if coverage.waiting_period is not None:
                item["waiting_period"] = {
                    "coverage_available_on": coverage.waiting_period.coverage_available_on.isoformat()
                }
            coverages[claim_type] = item

        return {
            "policy_id": self.policy_id,
            "policy_term": {
                "effective_from": self.term.effective_from.isoformat(),
                "effective_through": self.term.effective_through.isoformat(),
            },
            "coverages": coverages,
        }


def _parse_review_trigger(value: object, *, field: str) -> ReviewTrigger:
    mapping = _expect_mapping(value, field=field)
    _reject_unknown(mapping, allowed={"fact_type", "values"}, field=field)
    fact_type = _expect_str(mapping.get("fact_type"), field=f"{field}.fact_type")
    values = _expect_str_list(mapping.get("values"), field=f"{field}.values")
    if not values:
        raise PolicyValidationError(f"{field}.values must contain at least one value")
    return ReviewTrigger(fact_type=fact_type, values=frozenset(values))


def _parse_coverage(value: object, *, claim_type: str, field: str) -> CoverageRule:
    mapping = _expect_mapping(value, field=field)
    _reject_unknown(
        mapping,
        allowed={
            "enabled",
            "required_documents",
            "required_verified_facts",
            "review_triggers",
            "waiting_period",
        },
        field=field,
    )

    enabled_raw = mapping.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise PolicyValidationError(f"{field}.enabled must be a JSON boolean")

    required_documents = _expect_str_list(
        mapping.get("required_documents"), field=f"{field}.required_documents"
    )
    required_facts = _expect_str_list(
        mapping.get("required_verified_facts"),
        field=f"{field}.required_verified_facts",
    )

    triggers_raw = mapping.get("review_triggers", [])
    if not isinstance(triggers_raw, list):
        raise PolicyValidationError(f"{field}.review_triggers must be a JSON array")
    triggers = tuple(
        _parse_review_trigger(trigger, field=f"{field}.review_triggers[{index}]")
        for index, trigger in enumerate(triggers_raw)
    )

    waiting_period: WaitingPeriod | None = None
    waiting_raw = mapping.get("waiting_period")
    if waiting_raw is not None:
        waiting_map = _expect_mapping(waiting_raw, field=f"{field}.waiting_period")
        _reject_unknown(
            waiting_map,
            allowed={"coverage_available_on"},
            field=f"{field}.waiting_period",
        )
        coverage_available_on = _policy_date(
            waiting_map.get("coverage_available_on"),
            field=f"{field}.waiting_period.coverage_available_on",
        )
        waiting_period = WaitingPeriod(coverage_available_on=coverage_available_on)

    return CoverageRule(
        claim_type=claim_type,
        required_documents=required_documents,
        required_verified_facts=required_facts,
        review_triggers=triggers,
        waiting_period=waiting_period,
        enabled=enabled_raw,
    )


def parse_policy(document: object) -> Policy:
    """Validate a policy mapping and return an immutable :class:`Policy`."""

    root = _expect_mapping(document, field="policy")
    _reject_unknown(root, allowed={"policy_id", "policy_term", "coverages"}, field="policy")

    policy_id = _expect_str(root.get("policy_id"), field="policy.policy_id")

    term_map = _expect_mapping(root.get("policy_term"), field="policy.policy_term")
    _reject_unknown(
        term_map,
        allowed={"effective_from", "effective_through"},
        field="policy.policy_term",
    )
    effective_from = _policy_date(
        term_map.get("effective_from"), field="policy.policy_term.effective_from"
    )
    effective_through = _policy_date(
        term_map.get("effective_through"), field="policy.policy_term.effective_through"
    )
    if effective_from > effective_through:
        raise PolicyValidationError(
            "policy.policy_term.effective_from must be on or before effective_through"
        )

    coverages_map = _expect_mapping(root.get("coverages"), field="policy.coverages")
    if not coverages_map:
        raise PolicyValidationError("policy.coverages must define at least one coverage")

    coverages: dict[str, CoverageRule] = {}
    for raw_claim_type, raw_coverage in coverages_map.items():
        claim_type = _expect_str(raw_claim_type, field="policy.coverages key").lower()
        if claim_type in coverages:
            raise PolicyValidationError(
                f"policy.coverages contains duplicate claim type {claim_type!r}"
            )
        coverages[claim_type] = _parse_coverage(
            raw_coverage,
            claim_type=claim_type,
            field=f"policy.coverages.{claim_type}",
        )

    source_hash = sha256_hex(canonical_json_bytes(root))
    return Policy(
        policy_id=policy_id,
        term=PolicyTerm(
            effective_from=effective_from, effective_through=effective_through
        ),
        coverages=MappingProxyType(coverages),
        source_hash=source_hash,
    )


def default_policy_path() -> Path:
    """Return the shipped policy path, honoring ``CLAIM_GATE_POLICY``."""

    override = os.environ.get(DEFAULT_POLICY_ENV)
    if override:
        return Path(override).expanduser().resolve()
    # domain/policy.py -> domain -> claim_gate -> src -> plugin root
    return Path(__file__).resolve().parents[3] / "policies" / "auto_policy.json"


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and validate a JSON policy from disk.

    Any I/O, JSON, schema, or date error is raised as
    :class:`PolicyValidationError`; callers on the enforcement path must fail
    closed rather than continue with a partial policy.
    """

    policy_path = Path(path).expanduser() if path is not None else default_policy_path()
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyValidationError(f"unable to read policy file {policy_path}: {exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyValidationError(
            f"policy file {policy_path} is not valid JSON: {exc}"
        ) from exc

    try:
        return parse_policy(document)
    except ValidationError as exc:
        raise PolicyValidationError(f"invalid policy file {policy_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Slice 2: the pure 3-tier policy evaluation
# ---------------------------------------------------------------------------


def _block(code: str, message: str, *reasons: str) -> DecisionResult:
    return DecisionResult(
        decision=Decision.BLOCK,
        code=code,
        message=message,
        reasons=tuple(reasons),
    )


def _human_review(code: str, message: str, *reasons: str) -> DecisionResult:
    return DecisionResult(
        decision=Decision.HUMAN_REVIEW,
        code=code,
        message=message,
        reasons=tuple(reasons),
    )


def evaluate_claim(
    claim: NormalizedClaim,
    policy: Policy,
    resolved_evidence: Iterable[EvidenceLike] = (),
    resolved_facts: Iterable[FactLike] = (),
) -> DecisionResult:
    """Evaluate one claim package against the validated policy.

    The function is pure: the same claim, policy, and resolved rows always
    produce the same decision. It never reads the clock, the database, the
    filesystem, or any model prose.

    Precedence is BLOCK, then HUMAN_REVIEW, then PASS. Inside HUMAN_REVIEW
    the waiting period is reported first, then a missing required fact, then
    a matched review trigger; the first matched reason becomes the headline
    message and every additional detail is kept in ``reasons``.
    """

    # --- BLOCK: the policy itself does not cover this claim -------------
    if claim.policy_id != policy.policy_id:
        return _block(
            "POLICY_MISMATCH",
            (
                f"Policy mismatch: claim references {claim.policy_id} "
                f"but the active policy is {policy.policy_id}"
            ),
        )

    if not policy.is_effective_for(claim.incident_date):
        return _block(
            "POLICY_NOT_EFFECTIVE",
            (
                f"Policy {policy.policy_id} does not cover the incident date "
                f"{claim.incident_date.isoformat()}"
            ),
        )

    coverage = policy.coverage_for(claim.claim_type)
    if coverage is None:
        return _block(
            "NO_COVERAGE",
            f"No coverage rule for claim type: {claim.claim_type}",
        )

    if not coverage.enabled:
        return _block(
            "COVERAGE_DISABLED",
            f"Coverage is disabled for claim type: {claim.claim_type}",
        )

    # --- BLOCK: required documents ---------------------------------------
    documents: dict[str, EvidenceLike] = {}
    for record in resolved_evidence:
        documents.setdefault(record.document_type, record)

    missing_documents = [
        document_type
        for document_type in coverage.required_documents
        if document_type not in documents
    ]
    if missing_documents:
        return _block(
            "MISSING_DOCUMENT",
            f"Missing required document: {missing_documents[0]}",
            *(
                f"missing required document: {document_type}"
                for document_type in missing_documents
            ),
        )

    unverified_documents = [
        document_type
        for document_type in coverage.required_documents
        if documents[document_type].status != "verified"
    ]
    if unverified_documents:
        return _block(
            "DOCUMENT_NOT_VERIFIED",
            f"Unverified required document: {unverified_documents[0]}",
            *(
                f"unverified required document: {document_type}"
                for document_type in unverified_documents
            ),
        )

    # --- HUMAN_REVIEW: waiting period (incident-date only) ----------------
    if coverage.waiting_period is not None:
        waiting_period = coverage.waiting_period
        if not waiting_period.incident_is_eligible(claim.incident_date):
            return _human_review(
                "WAITING_PERIOD",
                "Review required: incident within waiting period",
                (
                    f"incident date {claim.incident_date.isoformat()} is before "
                    f"coverage availability "
                    f"{waiting_period.coverage_available_on.isoformat()}"
                ),
            )

    # --- HUMAN_REVIEW: required verified facts ----------------------------
    facts = tuple(resolved_facts)
    verified_facts = tuple(fact for fact in facts if fact.status == "verified")
    present_fact_types = {fact.fact_type for fact in verified_facts}

    unverified_fact_types = [
        fact_type
        for fact_type in coverage.required_verified_facts
        if fact_type not in present_fact_types
        and any(fact.fact_type == fact_type for fact in facts)
    ]
    if unverified_fact_types:
        return _block(
            "FACT_NOT_VERIFIED",
            f"Unverified required fact: {unverified_fact_types[0]}",
            *(
                f"unverified required fact: {fact_type}"
                for fact_type in unverified_fact_types
            ),
        )

    missing_fact_types = [
        fact_type
        for fact_type in coverage.required_verified_facts
        if fact_type not in present_fact_types
    ]
    if missing_fact_types:
        return _human_review(
            "MISSING_FACT",
            f"Review required: required fact missing: {missing_fact_types[0]}",
            *(
                f"missing required verified fact: {fact_type}"
                for fact_type in missing_fact_types
            ),
        )

    # --- HUMAN_REVIEW: review triggers ------------------------------------
    for trigger in coverage.review_triggers:
        for fact in verified_facts:
            if trigger.matches(fact.fact_type, fact.fact_value):
                return _human_review(
                    "REVIEW_TRIGGER",
                    f"Review required: {trigger.fact_type} is {fact.fact_value}",
                    (
                        f"review trigger matched: {trigger.fact_type}={fact.fact_value} "
                        f"({fact.fact_id})"
                    ),
                )

    # --- PASS --------------------------------------------------------------
    return DecisionResult(
        decision=Decision.PASS,
        code="PASS",
        message="All policy requirements satisfied",
        reasons=(
            f"resolved documents: {', '.join(sorted(documents))}",
            f"resolved facts: {', '.join(sorted(fact.fact_id for fact in facts))}",
        ),
    )
