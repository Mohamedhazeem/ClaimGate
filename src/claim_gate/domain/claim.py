"""Normalized claim domain model and strict date arithmetic.

The model is the anti-corruption boundary between untrusted model arguments
and ClaimGate's deterministic engine. It deliberately captures *references*
only:

    claim_id, policy_id, incident_date, claim_type,
    evidence_ids, incident_fact_ids

It never captures a verification status, document type, confidence score, or
operator name from the model. Those facts live in the trusted SQLite registry
and are resolved server-side by ``claim_gate.services.registry``.

All eligibility date arithmetic is performed against ``incident_date``. The
submission date is intentionally absent from this model, so it cannot leak
into waiting-period calculations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

__all__ = [
    "DateFormatError",
    "NormalizedClaim",
    "ValidationError",
    "normalize_claim",
    "parse_iso_date",
]

MAX_IDS = 100
MAX_IDENTIFIER_LENGTH = 128

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CLAIM_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ValidationError(ValueError):
    """Raised when a claim payload violates the normalized claim contract."""


class DateFormatError(ValidationError):
    """Raised when a date is not an exact, valid ``YYYY-MM-DD`` string."""


def parse_iso_date(value: object, *, field: str = "date") -> date:
    """Parse an exact ISO-8601 calendar date.

    The contract is intentionally narrower than ``date.fromisoformat``:
    only ``YYYY-MM-DD`` is accepted. Compact dates, datetimes, whitespace,
    timezone suffixes, and non-string values are rejected. Python's calendar
    validation still applies (for example, ``2026-02-30`` is rejected).
    """

    if not isinstance(value, str):
        raise DateFormatError(f"{field} must be a string in YYYY-MM-DD format")
    if not _DATE_RE.fullmatch(value):
        raise DateFormatError(f"{field} must use exact YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DateFormatError(f"{field} is not a valid calendar date: {value!r}") from exc


def _normalize_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    identifier = value.strip()
    if not identifier:
        raise ValidationError(f"{field} must not be empty")
    if len(identifier) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            f"{field} must be at most {MAX_IDENTIFIER_LENGTH} characters"
        )
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValidationError(
            f"{field} contains unsupported characters; "
            "use letters, digits, '.', '_', ':', or '-'"
        )
    return identifier


def _normalize_claim_type(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("claim_type must be a string")
    claim_type = value.strip().lower()
    if not _CLAIM_TYPE_RE.fullmatch(claim_type):
        raise ValidationError(
            "claim_type must be a lower-case policy key using letters, digits, '_' or '-'"
        )
    return claim_type


def _normalize_id_list(value: object, *, field: str) -> tuple[str, ...]:
    """Validate, de-duplicate, and sort a list of registry references.

    A set-like representation makes the package fingerprint independent of
    the order in which the model supplied IDs.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} must be a JSON array of strings")
    if len(value) > MAX_IDS:
        raise ValidationError(f"{field} must contain at most {MAX_IDS} identifiers")

    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        identifier = _normalize_identifier(raw, field=f"{field}[{index}]")
        if identifier not in seen:
            seen.add(identifier)
            normalized.append(identifier)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class NormalizedClaim:
    """An immutable, trust-free representation of a claim submission."""

    claim_id: str
    policy_id: str
    incident_date: date
    claim_type: str
    evidence_ids: tuple[str, ...]
    incident_fact_ids: tuple[str, ...]

    def is_coverage_available_on(self, coverage_available_on: date) -> bool:
        """Return whether the incident is outside the waiting period.

        Coverage becomes available on ``coverage_available_on`` inclusive.
        The comparison uses the incident date, never the submission date.
        """

        return self.incident_date >= coverage_available_on

    def waiting_days_remaining(self, coverage_available_on: date) -> int:
        """Calendar days from the incident to coverage availability.

        Returns zero when the incident is on or after the availability date.
        """

        return max(0, (coverage_available_on - self.incident_date).days)

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical, JSON-safe representation for hashing/display."""

        return {
            "claim_id": self.claim_id,
            "policy_id": self.policy_id,
            "incident_date": self.incident_date.isoformat(),
            "claim_type": self.claim_type,
            "evidence_ids": list(self.evidence_ids),
            "incident_fact_ids": list(self.incident_fact_ids),
        }


def normalize_claim(payload: object) -> NormalizedClaim:
    """Build a :class:`NormalizedClaim` from untrusted tool arguments.

    Unknown fields are deliberately ignored. In particular, model-supplied
    ``status``, ``verified``, ``verified_by``, ``document_type``, or
    ``confidence`` fields have no effect: the gate will only ever trust the
    registry lookup. This is a security boundary, not a permissive parser.
    """

    if not isinstance(payload, Mapping):
        raise ValidationError("claim arguments must be a JSON object")

    return NormalizedClaim(
        claim_id=_normalize_identifier(payload.get("claim_id"), field="claim_id"),
        policy_id=_normalize_identifier(payload.get("policy_id"), field="policy_id"),
        incident_date=parse_iso_date(
            payload.get("incident_date"), field="incident_date"
        ),
        claim_type=_normalize_claim_type(payload.get("claim_type")),
        evidence_ids=_normalize_id_list(
            payload.get("evidence_ids"), field="evidence_ids"
        ),
        incident_fact_ids=_normalize_id_list(
            payload.get("incident_fact_ids"), field="incident_fact_ids"
        ),
    )
