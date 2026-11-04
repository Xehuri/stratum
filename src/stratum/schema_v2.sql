-- Stratum schema v2
--
-- Adds first-class claims and claim relations on top of schema v1. Schema v1
-- DDL is never modified. Evidence continues to live in evidence_links with
-- owner_type = 'claim'; JSON payloads stay TEXT and no JSON1 functions are
-- required at runtime.

CREATE TABLE IF NOT EXISTS claims (
    id                    TEXT PRIMARY KEY,
    report_id             TEXT REFERENCES reports(id) ON DELETE SET NULL,
    claim_type            TEXT NOT NULL
                          CHECK (claim_type IN
                          ('observation', 'change', 'finding', 'pattern', 'hypothesis')),
    period_start          TEXT NOT NULL,
    period_end            TEXT NOT NULL,
    granularity           TEXT NOT NULL
                          CHECK (granularity IN
                          ('instant', 'day', 'week', 'month', 'range')),
    entities_json         TEXT NOT NULL,
    body                  TEXT NOT NULL,
    confidence            TEXT NOT NULL
                          CHECK (confidence IN ('high', 'medium', 'low')),
    status                TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN
                          ('draft', 'active', 'superseded', 'rejected', 'stale')),
    author                TEXT,
    contract_version      INTEGER NOT NULL DEFAULT 1 CHECK (contract_version >= 0),
    content_sha256        TEXT NOT NULL,
    data_gap              TEXT,
    metric                TEXT,
    baseline              TEXT,
    delta                 TEXT,
    sample_size           INTEGER CHECK (sample_size IS NULL OR sample_size >= 0),
    conditions            TEXT,
    outcome               TEXT,
    support_count         INTEGER CHECK (support_count IS NULL OR support_count >= 0),
    opportunity_count     INTEGER CHECK (opportunity_count IS NULL OR opportunity_count >= 0),
    counterexample_count  INTEGER CHECK (counterexample_count IS NULL OR counterexample_count >= 0),
    counterexample_note   TEXT,
    lag                   TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (period_end > period_start)
);

CREATE TABLE IF NOT EXISTS claim_relations (
    id             TEXT PRIMARY KEY,
    from_claim_id  TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    to_claim_id    TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation_type  TEXT NOT NULL
                   CHECK (relation_type IN
                   ('derived_from', 'supersedes', 'contradicts')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (from_claim_id, to_claim_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_claims_report ON claims(report_id);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_period ON claims(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_claim_relations_from ON claim_relations(from_claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_relations_to ON claim_relations(to_claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_relations_type ON claim_relations(relation_type);
