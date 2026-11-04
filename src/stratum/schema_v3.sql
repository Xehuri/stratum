-- Stratum schema v3
--
-- Adds the durable review audit trail. Schema v1 and v2 DDL are never
-- modified. Every promotion, rejection, reopen and supersede writes one row
-- here; the review state itself stays on claims.status and cards.status.
--
-- entity_type is 'claim' or 'card'. The table is intentionally not tied to
-- either table with a foreign key because the audit log must survive entity
-- mistakes and is polymorphic.

CREATE TABLE IF NOT EXISTS review_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('claim', 'card')),
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL
                CHECK (action IN ('promote', 'reject', 'reopen', 'supersede')),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    author      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_review_events_entity
    ON review_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_review_events_created
    ON review_events(created_at);
