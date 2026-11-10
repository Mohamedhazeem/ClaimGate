# ADR 001: Use DDD and Operator-Controlled Trusted Registries

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** ClaimGate architecture group
- **Supersedes:** None

## Context

ClaimGate sits between a language model and an insurance claim submission. The
model can propose a claim, but it cannot be trusted to establish that evidence
exists, that a document is verified, or that an incident fact is true. Model
output is probabilistic, can be prompt-injected by retrieved content, and can
hallucinate identifiers that look plausible.

The plugin must make a deterministic decision that an operator can audit:

- the same claim, policy, and registry state must produce the same outcome;
- a forged or stolen evidence ID must never reach the submission handler;
- a waiting period must be evaluated against the incident date;
- an operator must be able to release a reviewed package without giving the
  model an approval path.

Two design questions follow:

1. How do we keep policy and evidence decisions testable and independent of
   Hermes' runtime?
2. How do we make the trust boundary explicit so prompt injection cannot
   become a data-integrity bypass?

## Decision

### 1. Use Domain-Driven Design with a strict dependency rule

The code is split into four layers:

- **Domain** (`claim.py`, `policy.py`): immutable claim normalization, strict
  date parsing, policy schema, and the pure 3-tier rules. No SQLite, Hermes,
  network, filesystem, or clock dependencies.
- **Application services** (`registry.py`, `engine.py`, `audit.py`): trusted
  registry lookups, assessment orchestration, package hashing, audit, review,
  and ledger persistence.
- **Adapters** (`tools/`, `hooks/`, `cli/`): Hermes tool schemas/handlers, the
  fail-closed `pre_tool_call` hook, and operator CLI commands.
- **Composition root** (`__init__.py`): wiring only.

Dependencies point inward: domain ← services ← adapters. The domain contains
no infrastructure imports, so policy rules can be tested exhaustively and
reviewed independently of Hermes.

### 2. Keep the evidence registry outside the model boundary

The trusted registries are SQLite tables seeded and maintained by an operator:

- `evidence_registry` stores evidence IDs, claim ownership, document type,
  verification status, and the verifying operator.
- `incident_fact_registry` stores fact IDs, claim ownership, fact type, value,
  and verification status.

The model-facing schema accepts only references:

```text
claim_id, policy_id, incident_date, claim_type,
evidence_ids, incident_fact_ids
```

There is no model-facing tool that writes, verifies, or releases registry rows.
The gate resolves every reference and enforces existence, claim ownership, and
`status == 'verified'` before policy evaluation. A model-supplied `verified`
flag or a narrative claim such as "the police report is attached" has no
meaning.

This is the core prompt-injection defense: even if the model is manipulated
into asserting false evidence, the assertion is not an input to the decision.
The decision consumes only operator-controlled rows. Prompt injection can
change what the model *says*; it cannot change a SQLite row it has no write
path to.

### 3. Fail closed at the only interception point

`pre_tool_call` catches every ordinary error on the protected path and returns
an explicit Hermes block directive. A missing policy, database outage, malformed
argument, or audit failure is a block, not an allow. The gate returns `None`
only for a deterministic PASS (or a valid hash-bound operator release).

### 4. Bind human review to an immutable package hash

When a package needs human review, ClaimGate computes SHA-256 over canonical
JSON containing the normalized claim, policy hash, and every resolved evidence
and fact record. The exact hash is stored in `human_review_queue`.

The operator CLI (`hermes claimgate review-release <claim_id>
--package-hash <hash>`) releases only that hash. Any material change produces
a new hash, resets the queue entry to `pending`, and invalidates the old
release. The AI has no tool or slash command that can perform this action.

## Consequences

### Positive

- The trust boundary is explicit, testable, and independent of model behavior.
- Policy and evidence decisions are deterministic and can be replayed.
- A compromised or confused model cannot promote unverified evidence.
- The domain layer can be tested without Hermes, SQLite, or a clock.
- Operator releases are auditable and cannot silently survive package changes.
- A DB or configuration outage defaults to a safe block.

### Negative / trade-offs

- Operators must seed and maintain the registries; the plugin is not useful
  with an empty database.
- The gate may block legitimate claims during a database or configuration
  outage. That is intentional fail-closed behavior.
- Package hashing requires canonicalization discipline across all future
  fields; accidental inclusion of timestamps or session IDs would break
  release stability.
- The extra layers add ceremony compared with a single-file plugin. The
  separation is justified by the security and audit requirements.

## Alternatives considered

1. **Prompt-only guardrails.** Rejected: instructions are not an enforcement
   mechanism and can be overridden by injected content.
2. **Let the model self-verify and pass a `verified` boolean.** Rejected: the
   model is the untrusted party; a status argument is an assertion, not proof.
3. **Store evidence in model-provided JSON.** Rejected: no ownership check,
   no operator provenance, no durable audit.
4. **Use an external claims API.** Rejected for this plugin: the brief requires
   no external API calls and a local, deterministic decision.
5. **Single-layer handler checks.** Rejected: the hook is the interception
   point; putting the only check in the handler makes direct dispatch and
   future adapters unsafe. ClaimGate keeps both the hook and a defense-in-depth
   handler check.

## References

- `docs/plan/ClaimGate-Spec.md`
- `AGENTS.md`
- `src/claim_gate/services/schema.sql`
- `src/claim_gate/hooks/execution_gate.py`
