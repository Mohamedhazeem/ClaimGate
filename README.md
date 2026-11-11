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

> **Project status:** Slice 3 is implemented -- the final core slice. Domain
> contracts, strict date handling, policy loading and evaluation,
> trusted-registry lookups, package fingerprinting, audit/review persistence,
> the operator CLI, and the fail-closed hook are all in place.
> `DecisionEngine.evaluate` performs the 3-tier decision, so the gate allows a
> submission only on a deterministic PASS. A HUMAN_REVIEW package is queued
> with its immutable package hash; only an operator can release that exact
> hash through `hermes claimgate review-release`. The claim ledger is
> append-only and idempotent: a repeated submission returns
> `ALREADY_SUBMITTED` and never writes a second row.

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

The suite needs the Hermes host API (`plugins.plugin_storage`) for the checks
that touch the plugin database. It is located automatically, in order, from
`HERMES_AGENT`, `HERMES_HOME`, `~/.hermes/hermes-agent`, or an installed
`plugins` package. When no host is found, those checks **skip** rather than
fail, so the domain, policy, hashing, and date contracts still run on a bare
interpreter. Nothing is silenced: with a host present the full suite runs.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it does |
| --- | --- |
| `tests` | Byte-compiles the runtime package, runs the suite with no Hermes host (the host-dependent checks skip themselves), and rejects compiled payloads in the tracked tree. |
| `hosted` | Installs a pinned Hermes release, runs the **full** suite against the real host API, then runs `hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci`. |

The `hosted` job pins its Hermes release through the `HERMES_TAG` variable in
the workflow. The authoritative admission gate is still the catalog CI in
`NousResearch/hermes-agent`, which validates the pinned commit of this
repository.

### Demo

```bash
python3 demo/reset.py      # drop + recreate + seed the fixture
python3 demo/run_demo.py   # replay the 90-second storyboard; prints PASS
```

`demo/reset.py` restores the plugin's own trusted state: it drops and
recreates every ClaimGate table, re-seeds the operator registries, and
restores `policies/auto_policy.json`. `demo/run_demo.py` drives the real
gate, the real handler, and the real operator CLI through the whole
storyboard — missing document, forged ID, waiting period, operator CLI,
clean pass, idempotent resubmission, and the hash-bound release.

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
├── .github/
│   └── workflows/
│       └── ci.yml               # Tests + catalog gate on every push
├── docs/adr/
│   └── 001-use-ddd-and-trusted-registries.md
├── src/claim_gate/
│   ├── __init__.py              # Composition root (register)
│   ├── hosting.py               # Dev-only: locate the Hermes host
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
├── demo/
│   ├── reset.py                  # Drop + recreate + seed the fixture
│   └── run_demo.py               # Replay the 90-second storyboard
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
The plugin does not seed these tables at runtime either: `seed_database()` is
an explicit operator/demo action and is never called by `connect()` or the
gate, so a fresh install fails closed until an operator populates the
registries.

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
hermes claimgate review-release CLAIM-101 --hash <64-char-hash>
hermes claimgate ledger-list
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

## Disclosure (catalog submission)

What a user should know before installing ClaimGate:

* **Intercepts tool calls.** ClaimGate registers a `pre_tool_call` hook that
  intercepts `submit_claim_package` and may block it. No other tool is
  affected. The decision is deterministic and local.
* **Reads and writes its own database.** ClaimGate reads and writes
  `audit_log`, `human_review_queue`, and `claim_ledger`, and reads
  `evidence_registry` and `incident_fact_registry`. All live in the plugin's
  own SQLite file under `~/.hermes/plugin-data/claim_gate/data.db`. The
  plugin never reads another plugin's data or Hermes' `auth.json` / `.env`.
  The trusted registries are read-only at runtime: the plugin never seeds or
  writes them on the gate path. Seeding is an explicit operator/demo action
  (`demo/reset.py`).
* **Registers an operator CLI.** `hermes claimgate` exposes `review-list`,
  `review-release`, and `ledger-list`. It is registered through
  `ctx.register_cli_command()` and is never exposed as a model-facing tool.
* **Reads its own policy file.** `policies/auto_policy.json` is read at
  decision time. The `CLAIM_GATE_POLICY` environment variable can override
  the path.
* **No network, no shell, no background processes.** There are no HTTP
  calls, external APIs, model calls, subprocesses, or runtime third-party
  dependencies. The decision is deterministic and local.
* **No credentials.** ClaimGate declares no `requires_env` and reads no
  secret store. It does not touch OAuth tokens.
* **No self-update.** There is no in-app updater. Updates reach users only
  through the catalog's SHA-bump process.
* **Operator-only approval.** The only way to release a queued package is
  the `hermes claimgate review-release` CLI, which requires an exact
  package-hash match. The agent has no tool or slash command that can
  approve, release, or mutate a review row.

## What ClaimGate is not

- It is not a fraud model or a probability score.
- It does not read PDFs or validate document contents.
- It does not replace an adjuster, underwriter, or compliance review.
- It does not auto-approve because a model says evidence exists.
- It does not make network calls to a carrier or third-party service.

## License

MIT. See [LICENSE](LICENSE).
