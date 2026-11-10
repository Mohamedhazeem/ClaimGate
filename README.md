# ClaimGate: Evidence-Readiness Firewall

**The agent can prepare the claim. ClaimGate decides if the evidence is ready.**

ClaimGate is a native Hermes Agent plugin for property-and-casualty claim
operations. It intercepts model-initiated claim submissions before execution,
resolves every evidence and incident-fact reference against an
operator-controlled SQLite registry, evaluates a versioned policy, and returns
one deterministic outcome:

- **PASS** — every policy requirement is met; the submission handler executes.
- **BLOCK** — evidence is forged, missing, cross-claim, unverified, or the
  policy cannot be satisfied.
- **HUMAN_REVIEW** — the package is complete enough to queue, but a waiting
  period, review-triggering fact, or missing verified fact requires an operator.

The model never gets to verify its own evidence.

> **Project status:** Slice 2 is implemented. Domain contracts, strict date
> handling, policy loading and evaluation, trusted-registry lookups, package
> fingerprinting, audit/review persistence, the operator CLI, and the
> fail-closed hook are in place. `DecisionEngine.evaluate` now performs the
> 3-tier decision, so the gate allows a submission only on a deterministic
> PASS. Audit persistence, the review queue, and the operator CLI are wired
> but are still wired for Slice 3 (release flow) before real claims are
> approved.

## Installation

### Requirements

- Python 3.10 or newer.
- A Hermes Agent installation with native plugin support.
- No third-party runtime packages. ClaimGate uses the standard library;
  Hermes parses `plugin.yaml` (and supplies PyYAML for its own manifest path),
  while ClaimGate policies are JSON. ClaimGate does not import PyYAML itself.
  Hermes also provides the sanctioned plugin database helper.

### Development install

```bash
git clone https://github.com/Mohamedhazeem/ClaimGate.git claim-gate
cd claim-gate

# Link the checkout into the Hermes plugin directory.
ln -s "$(pwd)" "$HOME/.hermes/plugins/claim-gate"

# Validate the real runtime contracts before enabling.
hermes plugins doctor "$HOME/.hermes/plugins/claim-gate" --ci
hermes plugins enable claim-gate
hermes plugins list
```

If you copy instead of symlink, copy the whole directory including `src/` and
`policies/`.

### Local checks

```bash
python -m compileall -q src
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

`hermes plugins doctor` runs discovery, manifest parsing, `register(ctx)`, and
registry checks in a temporary Hermes home. It should not create any runtime
state in this repository.

## Architecture

ClaimGate follows Domain-Driven Design with a strict dependency direction:

```text
┌───────────────────────────────────────────────────────────────┐
│ Hermes adapters                                               │
│ tools/ · hooks/ · cli/                                        │
│ tool schemas, fail-closed gate, operator-only review commands │
└──────────────────────────────┬────────────────────────────────┘
                               │ depends on
┌──────────────────────────────▼────────────────────────────────┐
│ Application services                                          │
│ services/registry.py · engine.py · audit.py                  │
│ trusted lookups, assessment orchestration, hashing, SQLite    │
└──────────────────────────────┬────────────────────────────────┘
                               │ depends on
┌──────────────────────────────▼────────────────────────────────┐
│ Domain                                                        │
│ domain/claim.py · policy.py                                   │
│ normalized claim, strict dates, policy schema, 3-tier rules   │
└───────────────────────────────────────────────────────────────┘
```

The domain layer has no database, Hermes, HTTP, clock, or model dependency.
The services layer owns infrastructure adapters. Tools, hooks, and the CLI are
thin adapters around the same deterministic assessment path.

### Repository layout

```text
claim-gate/
├── plugin.yaml                  # Hermes manifest: tools + hooks
├── __init__.py                  # Hermes entry point and src/ path shim
├── pyproject.toml
├── .gitignore
├── AGENTS.md                    # Strict contributor/AI-agent contract
├── README.md
├── LICENSE
├── docs/adr/
│   └── 001-use-ddd-and-trusted-registries.md
├── src/claim_gate/
│   ├── __init__.py              # Composition root (register)
│   ├── domain/
│   │   ├── claim.py             # NormalizedClaim, strict date math
│   │   └── policy.py            # Policy schema, validation, policy hash
│   ├── services/
│   │   ├── registry.py          # SQLite evidence/fact lookups
│   │   ├── engine.py            # 3-tier evaluator + assessment seam
│   │   ├── audit.py             # Hashing, audit, review queue, ledger
│   │   └── schema.sql
│   ├── tools/
│   │   ├── submissions.py       # submit_claim_package handler
│   │   └── admin.py             # claim_check, policy_get, audit
│   ├── cli/
│   │   └── review.py            # review-list, review-release
│   └── hooks/
│       └── execution_gate.py    # pre_tool_call, post_tool_call
├── tests/
│   ├── unit/
│   └── integration/
└── policies/
    └── auto_policy.json
```

### Request lifecycle

```text
LLM proposes submit_claim_package(claim_id, policy_id, incident_date,
                                 claim_type, evidence_ids, incident_fact_ids)
        │
        ▼
Hermes pre_tool_call ── non-ClaimGate tool ──► allow (None)
        │
        ▼
normalize_claim (strict dates; status fields ignored)
        │
        ▼
evidence_registry + incident_fact_registry lookups
  missing / forged / cross-claim / unverified ──► BLOCK
        │
        ▼
Policy load + 3-tier DecisionEngine
  policy/evidence failure ──► BLOCK
  fact/trigger/waiting issue ──► HUMAN_REVIEW + package hash + review queue
  all satisfied ──► PASS
        │
        ▼
PASS requires a successful audit write; handler executes
        │
        ▼
post_tool_call records SUBMISSION_SUCCEEDED / SUBMISSION_FAILED
```

## Policy

The shipped policy lives at `policies/auto_policy.json` and can be overridden
with the `CLAIM_GATE_POLICY` environment variable (absolute path recommended).

```json
{
  "policy_id": "AUTO-001",
  "policy_term": {
    "effective_from": "2026-01-01",
    "effective_through": "2026-12-31"
  },
  "coverages": {
    "collision": {
      "required_documents": ["police_report", "repair_estimate"],
      "required_verified_facts": ["activity"],
      "review_triggers": [
        {"fact_type": "activity", "values": ["racing"]}
      ],
      "waiting_period": {"coverage_available_on": "2026-01-31"}
    }
  }
}
```

Rules:

- Unknown keys are rejected at every level. A policy typo fails loading rather
  than silently weakening a control.
- The effective term is inclusive on both dates.
- Waiting-period eligibility compares `incident_date` to
  `coverage_available_on`, never the submission date.
- The policy content hash is SHA-256 over canonical JSON and is bound into the
  package fingerprint.

## Trusted registry

The SQLite database is created through Hermes' `plugin_db()` helper at:

```text
~/.hermes/plugin-data/claim_gate/data.db
```

Schema ownership:

| Table | Purpose | Gate access |
| --- | --- | --- |
| `evidence_registry` | Operator-verified documents | Read only |
| `incident_fact_registry` | Operator-verified incident facts | Read only |
| `audit_log` | Tool-call decisions and outcomes | Insert/update |
| `claim_ledger` | Append-only accepted packages | Insert only |
| `human_review_queue` | Hash-bound operator review state | Insert/update via CLI |

Seed verified evidence and facts as an operator, for example:

```bash
sqlite3 "$HOME/.hermes/plugin-data/claim_gate/data.db" <<'SQL'
INSERT INTO evidence_registry
  (evidence_id, claim_id, document_type, status, verified_by)
VALUES
  ('EV-001', 'CLAIM-101', 'police_report', 'verified', 'operator@example'),
  ('EV-002', 'CLAIM-101', 'repair_estimate', 'verified', 'operator@example');

INSERT INTO incident_fact_registry
  (fact_id, claim_id, fact_type, fact_value, status)
VALUES
  ('FACT-001', 'CLAIM-101', 'activity', 'commuting', 'verified');
SQL
```

The model cannot write these tables. There is no verification tool, no
"mark verified" command, and no path from tool arguments into the registry.

## Tools

| Tool | Purpose |
| --- | --- |
| `submit_claim_package` | The gated submission. Accepts references only; a PASS writes the immutable ledger entry. |
| `claim_check` | Dry-run the same assessment without submitting, queueing review, or writing the ledger. |
| `claim_policy_get` | Return the validated active policy and content hash. |
| `claim_audit` | Read audit history and review state for one claim. |

Tool schemas deliberately expose only `claim_id`, `policy_id`,
`incident_date`, `claim_type`, `evidence_ids`, and `incident_fact_ids`.
Verification status and document type are not model inputs.

## Operator review CLI

The AI has no approval path. An operator uses:

```bash
hermes claimgate review-list
hermes claimgate review-list --all --json
hermes claimgate review-release CLAIM-101 --package-hash <64-char-hash>
```

A release is accepted only when `claim_id` and the exact package hash match the
queue row. If the AI changes a document, fact, claim field, or the policy, the
package hash changes, the queue resets to `pending`, and the old release is
invalid.

## Security boundaries

### 1. Reference-only model surface

The model sends IDs, not truth. It cannot supply `status`, `verified`,
`document_type`, `confidence`, or `verified_by`. Those values come from the
operator-controlled database.

### 2. Fail-closed interception

`pre_tool_call` wraps the protected path in exception handling and returns an
explicit block for malformed input, policy failure, registry failure, database
failure, audit failure, and every unexpected error. A broken gate is a closed
gate.

### 3. Immutable evidence and policy binding

`package_hash` is SHA-256 over canonical JSON containing:

- the normalized claim;
- the policy content hash;
- every resolved evidence record and its per-record hash;
- every resolved incident fact and its per-record hash.

Ordering is canonicalized. Any material change produces a different hash.

### 4. Human authority is hash-bound

The review queue stores the exact reviewed hash. `review-release` requires that
hash. The agent cannot call the CLI, and no model-facing tool wraps it.

### 5. Strict date semantics

Only exact `YYYY-MM-DD` calendar dates are accepted. Waiting periods are
computed from the incident date, not the submission date, so delayed
submissions cannot change eligibility.

### 6. No external side channels

There are no HTTP calls, external APIs, model calls, subprocesses, or runtime
third-party dependencies. The decision is deterministic and local.

### 7. Auditability

`audit_log` records the tool call decision and final submission outcome.
`claim_ledger` is append-only and protected by update/delete triggers. The
operator review queue records pending/released state and timestamps.

## What ClaimGate is not

- It is not a fraud model or a probability score.
- It does not read PDFs or validate document contents.
- It does not replace an adjuster, underwriter, or compliance review.
- It does not auto-approve because a model says evidence exists.
- It does not make network calls to a carrier or third-party service.

## License

MIT. See [LICENSE](LICENSE).
