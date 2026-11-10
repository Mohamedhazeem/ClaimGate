-- ClaimGate trusted state.
--
-- Ownership:
--   * evidence_registry and incident_fact_registry are operator-controlled
--     trust anchors. No model-facing tool may write to them.
--   * audit_log is append/update-only operational evidence keyed by the
--     Hermes tool_call_id.
--   * claim_ledger is append-only; triggers forbid UPDATE and DELETE.
--   * human_review_queue binds a pending release to the exact package hash
--     the operator reviewed.
--
-- All timestamps are UTC ISO-8601 text. This schema is idempotent and is
-- applied by services/registry.py through Hermes' plugin_db() connection.

CREATE TABLE IF NOT EXISTS evidence_registry (
    evidence_id   TEXT PRIMARY KEY,
    claim_id      TEXT NOT NULL,
    document_type TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('verified', 'pending', 'rejected', 'revoked')),
    verified_by   TEXT,
    CHECK (length(trim(evidence_id)) > 0),
    CHECK (length(trim(claim_id)) > 0),
    CHECK (length(trim(document_type)) > 0),
    CHECK (status <> 'verified'
           OR (verified_by IS NOT NULL AND length(trim(verified_by)) > 0))
);

CREATE INDEX IF NOT EXISTS idx_evidence_registry_claim
    ON evidence_registry (claim_id, status);

CREATE TABLE IF NOT EXISTS incident_fact_registry (
    fact_id    TEXT PRIMARY KEY,
    claim_id   TEXT NOT NULL,
    fact_type  TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    status     TEXT NOT NULL
               CHECK (status IN ('verified', 'pending', 'rejected', 'revoked')),
    CHECK (length(trim(fact_id)) > 0),
    CHECK (length(trim(claim_id)) > 0),
    CHECK (length(trim(fact_type)) > 0),
    CHECK (length(trim(fact_value)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_incident_fact_registry_claim
    ON incident_fact_registry (claim_id, status);

CREATE TABLE IF NOT EXISTS audit_log (
    tool_call_id TEXT PRIMARY KEY,
    claim_id     TEXT NOT NULL,
    decision     TEXT NOT NULL
                 CHECK (decision IN (
                     'PASS',
                     'BLOCK',
                     'HUMAN_REVIEW',
                     'SUBMISSION_SUCCEEDED',
                     'SUBMISSION_FAILED'
                 )),
    package_hash TEXT
                 CHECK (package_hash IS NULL OR length(package_hash) = 64),
    timestamp    TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(trim(tool_call_id)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_audit_log_claim_time
    ON audit_log (claim_id, timestamp);

CREATE TABLE IF NOT EXISTS claim_ledger (
    claim_id     TEXT PRIMARY KEY,
    package_hash TEXT NOT NULL CHECK (length(package_hash) = 64),
    timestamp    TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (length(trim(claim_id)) > 0)
);

CREATE TRIGGER IF NOT EXISTS claim_ledger_no_update
BEFORE UPDATE ON claim_ledger
BEGIN
    SELECT RAISE(ABORT, 'claim_ledger records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS claim_ledger_no_delete
BEFORE DELETE ON claim_ledger
BEGIN
    SELECT RAISE(ABORT, 'claim_ledger records are immutable');
END;

CREATE TABLE IF NOT EXISTS human_review_queue (
    claim_id     TEXT PRIMARY KEY,
    package_hash TEXT NOT NULL CHECK (length(package_hash) = 64),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'released')),
    created_at   TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at  TEXT,
    CHECK (length(trim(claim_id)) > 0),
    CHECK (status <> 'released' OR released_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_human_review_queue_status
    ON human_review_queue (status, created_at);

PRAGMA user_version = 1;
