"""Policy schema, strict JSON loading, and deterministic policy hashing.

The policy is a versioned operator artifact, not model input. It is loaded
from ``policies/auto_policy.json`` (or ``CLAIM_GATE_POLICY``) and validated
before use. Unknown keys are rejected at every level so a typo such as
``required_document`` cannot silently remove a control.

This module stays in the domain layer: it has no database or Hermes imports.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .claim import ValidationError, parse_iso_date

__all__ = [
    "CoverageRule",
    "Policy",
    "PolicyTerm",
    "PolicyValidationError",
    "ReviewTrigger",
    "WaitingPeriod",
    "canonical_json_bytes",
    "default_policy_path",
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


@dataclass(frozen=True, slots=True)
class CoverageRule:
    """Deterministic requirements for one claim type."""

    claim_type: str
    required_documents: frozenset[str]
    required_verified_facts: frozenset[str]
    review_triggers: tuple[ReviewTrigger, ...]
    waiting_period: WaitingPeriod | None


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
            "required_documents",
            "required_verified_facts",
            "review_triggers",
            "waiting_period",
        },
        field=field,
    )

    required_documents = frozenset(
        _expect_str_list(
            mapping.get("required_documents"), field=f"{field}.required_documents"
        )
    )
    required_facts = frozenset(
        _expect_str_list(
            mapping.get("required_verified_facts"),
            field=f"{field}.required_verified_facts",
        )
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
