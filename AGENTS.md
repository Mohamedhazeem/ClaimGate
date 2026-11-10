# AGENTS.md — ClaimGate Engineering Contract

This repository is a native Hermes Agent plugin that enforces a regulated
evidence-readiness boundary for insurance claim submissions. Treat it as
security-sensitive production code, not a demo.

> **Current scaffold state:** domain models, policy loading and evaluation,
> trusted-registry lookups, package hashing, audit/review persistence, the
> operator CLI, and the fail-closed hook are wired. Slice 2 is implemented:
> `DecisionEngine.evaluate()` delegates to the pure
> `domain/policy.py::evaluate_claim()`, so a protected submission is allowed
> only on a deterministic PASS. Slice 3 (audit rows, review queue, release
> flow) is still outstanding; do not "temporarily" bypass the gate to make a
> demo work.

## 1. Non-negotiable security invariants

### 1.1 Trust boundary: the model is never a verification source

The ONLY claim fields accepted from model arguments are:

```text
claim_id
policy_id
incident_date
claim_type
evidence_ids
incident_fact_ids
```

`evidence_ids` and `incident_fact_ids` are **references**, not claims of truth.

- NEVER trust a verification status, document type, `verified` flag, confidence
  score, `verified_by` value, fact value, or operator name supplied by the model.
- NEVER derive evidence readiness from model prose, tool narration, or a chat
  message. Only `evidence_registry` and `incident_fact_registry` rows may
  establish trusted facts.
- The system MUST resolve every supplied ID against SQLite and verify:
  1. the row exists;
  2. `claim_id` matches the submitted claim;
  3. `status == 'verified'`.
- Unknown fields in tool arguments are ignored by
  `domain/claim.py::normalize_claim`. Do not change this to read model-supplied
  status fields, and do not add status fields to a tool schema.
- Do not add any model-facing tool that inserts, updates, deletes, verifies, or
  releases registry rows. Registry seeding is an operator action.
- If a requirement seems to need model trust, STOP and redesign it around an
  operator-controlled ID.

### 1.2 Fail-closed is the default

`pre_tool_call` in `src/claim_gate/hooks/execution_gate.py` is the enforcement
point for `submit_claim_package`.

- It MUST catch every ordinary exception on the protected path and explicitly
  return:

  ```python
  {"action": "block", "message": "..."}
  ```

- Never return `None` (allow) from an error path.
- Never catch an exception and continue to handler execution.
- Treat these as blocks: malformed args, missing policy, invalid policy,
  registry lookup failure, database error, package-hash failure, audit failure,
  timeout, and any unexpected exception.
- Only `submit_claim_package` is protected by the gate. Do not block unrelated
  tools. Non-protected tools return `None`.
- `post_tool_call` is an observer. It may log, but it must never be required for
  a block decision and must not raise into the agent loop.
- A database/audit outage is a block, not a bypass.

### 1.3 Dates are strict, and waiting periods belong to the incident

- Use `datetime.date` exclusively for eligibility dates. Do not use
  `datetime.datetime`, `time`, floats, epoch seconds, or naive timestamps.
- Parse only exact `YYYY-MM-DD` strings through
  `domain/claim.py::parse_iso_date`. Reject malformed strings, datetimes,
  whitespace, timezone suffixes, and impossible calendar dates.
- Waiting periods MUST be calculated against `incident_date`. NEVER use the
  submission date, `date.today()`, or a wall clock for eligibility.
- Coverage becomes available on `coverage_available_on` inclusive. The waiting
  period is not a function of when the AI happened to submit.
- Do not use `dateutil`, `pytz`, or any third-party date library.

### 1.4 The AI cannot approve or release its own claim

- Human review is exposed only through
  `ctx.register_cli_command("claimgate", ...)` and
  `src/claim_gate/cli/review.py`.
- There MUST be no model-facing tool, slash command, or hook path that approves,
  releases, or mutates a review row.
- A release MUST be bound to the exact immutable `package_hash` the operator
  reviewed. A mismatch is a rejection, never a warning.
- Any change to the claim, policy, evidence rows, facts, or registry state that
  feeds the fingerprint changes the hash. The old release becomes invalid and
  the package returns to `pending`.
- Never auto-release based on time, amount, confidence, or model request.
- Never store a boolean "approved" flag detached from the package hash.

### 1.5 SQLite access goes through Hermes

- Use `sqlite3` exclusively through the provided Hermes `plugin_db()` helper
  (`plugins.plugin_storage.plugin_db`), wrapped by
  `services/registry.py::connect()`.
- NEVER call `sqlite3.connect(path)`, open `.claim_gate/data.db`, or write a
  database into the plugin install directory.
- Runtime state belongs in `~/.hermes/plugin-data/claim-gate/data.db` (managed
  by Hermes and profile-aware).
- Apply schema changes in `services/schema.sql`; keep them idempotent. Do not
  create schema ad hoc in handlers.
- Use parameterized SQL only. Never concatenate model-supplied values into SQL.
- Keep writes transactional. Never write to `evidence_registry` or
  `incident_fact_registry` from the gate. `claim_ledger` is append-only and
  protected by triggers.

## 2. Determinism and dependency rules

- Runtime code is Python 3.10+ standard library only. The policy is JSON;
  do not add YAML parsing to the runtime path.
- NO third-party runtime dependencies. NO network calls, HTTP clients,
  telemetry, external APIs, model calls, subprocesses, `eval`, `exec`, or
  pickle.
- Decisions must be a pure function of the normalized claim, validated policy,
  and resolved SQLite rows. No randomness, clock, locale, environment, or
  model-text dependence in the decision.
- The package fingerprint excludes wall-clock time and session IDs. Preserve
  that determinism.
- Dependency direction:
  - `domain/` MUST NOT import `services/`, `tools/`, `hooks/`, or `cli/`.
  - `services/` may import `domain/`.
  - `tools/`, `hooks/`, and `cli/` may import `domain/` and `services/`.
  - `__init__.py` is composition only; keep business rules out of it.
- Keep `submit_claim_package` schema and handler free of verification fields.

## 3. Tool and hook contracts

- Tool handlers have the signature `def handler(args: dict, **kwargs) -> str`.
- Always return a JSON string, on success and on error. Never raise to Hermes.
- Hook callbacks accept `**kwargs` for forward compatibility.
- `pre_tool_call` returns `None` only when the protected claim is allowed; it
  returns a block directive for every denial.
- `post_tool_call` returns `None` and never vetoes.
- `plugin.yaml` MUST list every registered tool under `provides_tools` and every
  registered hook under `provides_hooks`.
- Do not register the review CLI as a tool. CLI is operator-only.

## 4. Implementation boundaries

- `domain/claim.py`: normalization, strict dates, incident-date math.
- `domain/policy.py`: policy schema, validation, canonical hash, and the pure
  `evaluate_claim()` 3-tier evaluation.
- `services/registry.py`: trusted SQLite lookups and schema bootstrap.
- `services/engine.py`: 3-tier evaluation adapter plus assessment composition.
- `services/audit.py`: package hash, audit, review queue, ledger.
- `tools/submissions.py`: model-facing submission adapter.
- `tools/admin.py`: `claim_check`, `claim_policy_get`, `claim_audit`.
- `hooks/execution_gate.py`: enforcement adapters.
- `cli/review.py`: operator-only review list/release.

Slice 2 landed as a pure `domain/policy.py::evaluate_claim()` with
`services/engine.py::DecisionEngine.evaluate()` as a thin adapter. The policy
rules must not move into the hook, and they must not be duplicated in the
handler. The precedence is:

1. **BLOCK** — policy mismatch/expired term/unknown coverage/disabled
   coverage; missing or unverified required document; forged, missing,
   cross-claim, or unverified reference.
2. **HUMAN_REVIEW** — incident date inside the waiting period; required
   verified fact missing; verified fact matches a review trigger.
3. **PASS** — all requirements satisfied and no trigger matched.

## 5. Change checklist

Before considering any change complete:

- [ ] No model-supplied verification/status field is trusted.
- [ ] Every protected failure path returns an explicit block.
- [ ] Waiting-period math uses `incident_date` only.
- [ ] No new runtime dependency or network call.
- [ ] Schema changes are idempotent and in `services/schema.sql`.
- [ ] `plugin.yaml` declarations still match registrations.
- [ ] Package hash changes when evidence, facts, claim, or policy change.
- [ ] Human release remains hash-bound and operator-only.
- [ ] Tests cover the forged/cross-claim/missing/pending/trigger/waiting cases.

## 6. Commands

Run these from the repository root:

```bash
python -m compileall -q src
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

`hermes plugins doctor` uses a temporary Hermes home. It must pass without
creating runtime state in the plugin directory.

## 7. Forbidden shortcuts

- Do not "just check the model's `verified` field".
- Do not treat a chat assertion like "the police report is attached" as evidence.
- Do not skip the database lookup for speed.
- Do not return `{"action": "approve"}` as a substitute for a deterministic
  PASS. ClaimGate either deterministically allows, blocks, or queues for a
  human.
- Do not release a claim by claim ID alone. The hash is mandatory.
- Do not log secrets, full policy payloads, or unnecessary personal data.
- Do not silence a failing test or broaden an exception handler to make a test
  pass.
