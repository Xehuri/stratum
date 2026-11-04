-- Stratum schema v1
--
-- The Markdown source is the only source of truth. This database is a
-- rebuildable index plus history that must not be lost. JSON payloads are
-- stored as TEXT and validated by the application; no JSON1 functions are
-- required at runtime.
--
-- Timestamps are ISO-8601 UTC text. Intervals use half-open [start, end).

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Content: source anchors, snapshots and evidence
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS blobs (
    id          TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,
    media_type  TEXT NOT NULL DEFAULT 'text/plain',
    size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
    content     BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    path             TEXT NOT NULL UNIQUE,
    title            TEXT,
    retrieval_type   TEXT NOT NULL DEFAULT 'structured'
                     CHECK (retrieval_type IN ('structured', 'prose', 'legacy')),
    contract_version INTEGER NOT NULL DEFAULT 0 CHECK (contract_version >= 0),
    frontmatter_json TEXT,
    source_sha256    TEXT,
    size_bytes       INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mtime            TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS document_versions (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL CHECK (version >= 1),
    blob_id      TEXT REFERENCES blobs(id),
    source_sha256 TEXT NOT NULL,
    line_count   INTEGER CHECK (line_count IS NULL OR line_count >= 0),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, version)
);

-- ---------------------------------------------------------------------------
-- Metadata: versions, configuration, reproducible segmentation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspace_meta (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    workspace_id   TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    root           TEXT NOT NULL,
    db_path        TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    copied_from    TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    description TEXT,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS segmentation_runs (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version_id TEXT REFERENCES document_versions(id),
    profile_id          TEXT REFERENCES profiles(id),
    selector_count      INTEGER CHECK (selector_count IS NULL OR selector_count >= 0),
    block_count         INTEGER NOT NULL DEFAULT 0 CHECK (block_count >= 0),
    status              TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('running', 'completed', 'failed')),
    options_json        TEXT,
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Content: blocks and lineage
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS blocks (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version_id TEXT REFERENCES document_versions(id),
    profile_id          TEXT REFERENCES profiles(id),
    parent_block_id     TEXT REFERENCES blocks(id),
    retrieval_type      TEXT NOT NULL
                        CHECK (retrieval_type IN ('structured', 'prose', 'legacy')),
    selector_type       TEXT NOT NULL,
    anchor              TEXT NOT NULL,
    occurrence          INTEGER NOT NULL DEFAULT 0 CHECK (occurrence >= 0),
    line_start          INTEGER NOT NULL CHECK (line_start >= 1),
    line_end            INTEGER NOT NULL CHECK (line_end >= line_start),
    snapshot_text       TEXT NOT NULL,
    snapshot_sha256     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'stale', 'missing')),
    legacy_block_id     TEXT UNIQUE,
    metadata_json       TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS block_lineage (
    id           TEXT PRIMARY KEY,
    old_block_id TEXT NOT NULL,
    new_block_id TEXT NOT NULL,
    reason       TEXT NOT NULL
                 CHECK (reason IN ('split', 'merge', 'rename', 'move', 'profile_change')),
    method       TEXT NOT NULL CHECK (method IN ('anchor', 'snapshot', 'manual')),
    confidence   REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (old_block_id, new_block_id, reason)
);

-- ---------------------------------------------------------------------------
-- Index: concepts, cards, relations, reports
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS concepts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT REFERENCES concepts(id),
    path        TEXT NOT NULL UNIQUE,
    is_leaf     INTEGER NOT NULL DEFAULT 0 CHECK (is_leaf IN (0, 1)),
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS cards (
    id             TEXT PRIMARY KEY,
    card_type      TEXT NOT NULL
                   CHECK (card_type IN ('event', 'observation', 'finding', 'state', 'decision')),
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'active', 'superseded', 'rejected', 'stale')),
    claim_type     TEXT CHECK (claim_type IS NULL OR claim_type IN
                   ('observation', 'finding', 'pattern', 'change', 'hypothesis')),
    confidence     TEXT CHECK (confidence IS NULL OR confidence IN ('high', 'medium', 'low')),
    period_start   TEXT,
    period_end     TEXT,
    granularity    TEXT CHECK (granularity IS NULL OR granularity IN
                   ('instant', 'day', 'week', 'month', 'range')),
    valid_from     TEXT,
    valid_to       TEXT,
    legacy_card_id TEXT UNIQUE,
    superseded_by  TEXT REFERENCES cards(id),
    author         TEXT,
    metadata_json  TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS card_concepts (
    card_id    TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (card_id, concept_id)
);

CREATE TABLE IF NOT EXISTS card_blocks (
    card_id      TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    block_id     TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    quote_sha256 TEXT,
    role         TEXT NOT NULL DEFAULT 'evidence',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (card_id, block_id)
);

CREATE TABLE IF NOT EXISTS card_relations (
    id                TEXT PRIMARY KEY,
    from_card_id      TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    to_card_id        TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    relation_type     TEXT NOT NULL,
    claim_type        TEXT CHECK (claim_type IS NULL OR claim_type IN
                       ('temporal', 'causal', 'associative')),
    source            TEXT NOT NULL DEFAULT 'manual'
                      CHECK (source IN ('auto', 'manual', 'agent')),
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    weight            REAL,
    evidence_links_json TEXT,
    valid_from        TEXT,
    valid_to          TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (from_card_id, to_card_id, relation_type)
);

CREATE TABLE IF NOT EXISTS reports (
    id               TEXT PRIMARY KEY,
    report_type      TEXT NOT NULL
                     CHECK (report_type IN ('daily', 'weekly', 'biweekly', 'summary')),
    title            TEXT NOT NULL,
    period_start     TEXT,
    period_end       TEXT,
    author           TEXT,
    contract_version INTEGER NOT NULL DEFAULT 0 CHECK (contract_version >= 0),
    base_commit      TEXT,
    document_id      TEXT REFERENCES documents(id),
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft', 'active', 'stale', 'archived')),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS report_blocks (
    report_id  TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    block_id   TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'evidence',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (report_id, block_id)
);

CREATE TABLE IF NOT EXISTS report_cards (
    report_id  TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    card_id    TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'reference',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (report_id, card_id)
);

CREATE TABLE IF NOT EXISTS report_relations (
    id             TEXT PRIMARY KEY,
    from_report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    to_report_id   TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    relation_type  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (from_report_id, to_report_id, relation_type)
);

-- ---------------------------------------------------------------------------
-- Observation: metrics, observations, states, change points, patterns
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS series (
    id          TEXT PRIMARY KEY,
    metric_id   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    unit        TEXT,
    value_type  TEXT NOT NULL CHECK (value_type IN ('int', 'float', 'duration', 'money', 'count')),
    entity_type TEXT NOT NULL DEFAULT 'self',
    aggregation TEXT NOT NULL DEFAULT 'avg'
                CHECK (aggregation IN ('sum', 'avg', 'min', 'max', 'last')),
    direction   TEXT NOT NULL DEFAULT 'neutral'
                CHECK (direction IN ('higher_better', 'lower_better', 'neutral')),
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS metric_points (
    id          TEXT PRIMARY KEY,
    series_id   TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL DEFAULT 'self',
    entity_id   TEXT NOT NULL DEFAULT 'self',
    ts          TEXT NOT NULL,
    value       REAL,
    value_text  TEXT,
    source      TEXT,
    metadata_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (value IS NOT NULL OR value_text IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS observations (
    id                 TEXT PRIMARY KEY,
    series_id          TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    metric_id          TEXT NOT NULL,
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    granularity        TEXT NOT NULL DEFAULT 'day'
                       CHECK (granularity IN ('instant', 'day', 'week', 'month', 'range')),
    value              REAL,
    baseline           REAL,
    delta              REAL,
    sample_size        INTEGER CHECK (sample_size IS NULL OR sample_size >= 0),
    confidence         REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'active', 'stale')),
    evidence_links_json TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (period_end >= period_start),
    UNIQUE (metric_id, granularity, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS states (
    id                 TEXT PRIMARY KEY,
    state_type         TEXT NOT NULL,
    entity_type        TEXT NOT NULL DEFAULT 'self',
    entity_id          TEXT NOT NULL DEFAULT 'self',
    start_at           TEXT NOT NULL,
    end_at             TEXT,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'ended', 'stale')),
    severity           TEXT,
    evidence_links_json TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (end_at IS NULL OR end_at >= start_at)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id          TEXT PRIMARY KEY,
    state_id    TEXT NOT NULL REFERENCES states(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS change_points (
    id                 TEXT PRIMARY KEY,
    series_id          TEXT REFERENCES series(id) ON DELETE CASCADE,
    metric_id          TEXT NOT NULL,
    effective_at       TEXT NOT NULL,
    before_value       REAL,
    after_value        REAL,
    effect_size        REAL,
    confidence         REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    method             TEXT NOT NULL,
    evidence_links_json TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS patterns (
    id                        TEXT PRIMARY KEY,
    claim_type                TEXT NOT NULL
                              CHECK (claim_type IN
                              ('association', 'causal', 'temporal', 'cycle', 'threshold')),
    name                      TEXT NOT NULL,
    conditions_json           TEXT NOT NULL,
    outcome                   TEXT NOT NULL,
    support_count             INTEGER NOT NULL DEFAULT 0 CHECK (support_count >= 0),
    opportunity_count         INTEGER NOT NULL DEFAULT 0 CHECK (opportunity_count >= 0),
    counterexample_count      INTEGER NOT NULL DEFAULT 0 CHECK (counterexample_count >= 0),
    counterexample_links_json TEXT,
    period_start              TEXT,
    period_end                TEXT,
    confidence                REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    status                    TEXT NOT NULL DEFAULT 'candidate'
                              CHECK (status IN ('candidate', 'active', 'deprecated')),
    last_verified_at          TEXT,
    evidence_links_json       TEXT,
    created_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pattern_instances (
    id                 TEXT PRIMARY KEY,
    pattern_id         TEXT NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
    entity_type        TEXT NOT NULL DEFAULT 'self',
    entity_id          TEXT NOT NULL DEFAULT 'self',
    occurred_at        TEXT NOT NULL,
    outcome            TEXT,
    evidence_links_json TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id           TEXT PRIMARY KEY,
    owner_type   TEXT NOT NULL,
    owner_id     TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    quote_sha256 TEXT,
    role         TEXT NOT NULL DEFAULT 'evidence',
    confidence   REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (owner_type, owner_id, target_type, target_id, role)
);

-- ---------------------------------------------------------------------------
-- History: linear commits, pending changes, entity versions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS commits (
    id             TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL UNIQUE CHECK (seq >= 1),
    parent_id      TEXT REFERENCES commits(id),
    author         TEXT NOT NULL,
    message        TEXT NOT NULL,
    change_count   INTEGER NOT NULL DEFAULT 0 CHECK (change_count >= 0),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS entity_versions (
    id          TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    commit_id   TEXT REFERENCES commits(id),
    blob_id     TEXT REFERENCES blobs(id),
    data_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (entity_type, entity_id, version)
);

CREATE TABLE IF NOT EXISTS changes (
    id                TEXT PRIMARY KEY,
    commit_id         TEXT REFERENCES commits(id) ON DELETE SET NULL,
    entity_type       TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    op                TEXT NOT NULL CHECK (op IN ('create', 'update', 'delete', 'restore')),
    before_version_id TEXT REFERENCES entity_versions(id),
    after_version_id  TEXT REFERENCES entity_versions(id),
    before_json       TEXT,
    after_json        TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS workspace_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Operations: imports, legacy id map, diagnostics
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS import_runs (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT,
    stats_json  TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS legacy_id_map (
    id          TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    legacy_id   TEXT NOT NULL,
    new_id      TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (entity_type, legacy_id)
);

CREATE TABLE IF NOT EXISTS fsck_runs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'clean', 'failed')),
    options_json  TEXT,
    finding_count INTEGER NOT NULL DEFAULT 0 CHECK (finding_count >= 0),
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS fsck_findings (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES fsck_runs(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    entity_type TEXT,
    entity_id   TEXT,
    message     TEXT NOT NULL,
    details_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Search: FTS5 derived indexes (standalone tables, populated by the app)
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    block_id UNINDEXED,
    document_id UNINDEXED,
    snapshot_text,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    card_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    observation_id UNINDEXED,
    metric_id UNINDEXED,
    summary,
    tokenize = 'unicode61'
);

-- ---------------------------------------------------------------------------
-- Query indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_documents_retrieval_type ON documents(retrieval_type);
CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path);
CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_segmentation_runs_document ON segmentation_runs(document_id);
CREATE INDEX IF NOT EXISTS idx_blocks_document ON blocks(document_id);
CREATE INDEX IF NOT EXISTS idx_blocks_status ON blocks(status);
CREATE INDEX IF NOT EXISTS idx_blocks_parent ON blocks(parent_block_id);
CREATE INDEX IF NOT EXISTS idx_block_lineage_old ON block_lineage(old_block_id);
CREATE INDEX IF NOT EXISTS idx_block_lineage_new ON block_lineage(new_block_id);
CREATE INDEX IF NOT EXISTS idx_concepts_parent ON concepts(parent_id);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_type ON cards(card_type);
CREATE INDEX IF NOT EXISTS idx_cards_period ON cards(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_card_blocks_block ON card_blocks(block_id);
CREATE INDEX IF NOT EXISTS idx_card_relations_to ON card_relations(to_card_id);
CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type);
CREATE INDEX IF NOT EXISTS idx_report_blocks_block ON report_blocks(block_id);
CREATE INDEX IF NOT EXISTS idx_report_cards_card ON report_cards(card_id);
CREATE INDEX IF NOT EXISTS idx_report_relations_to ON report_relations(to_report_id);
CREATE INDEX IF NOT EXISTS idx_metric_points_series_ts ON metric_points(series_id, ts);
CREATE INDEX IF NOT EXISTS idx_observations_metric_period ON observations(metric_id, period_start);
CREATE INDEX IF NOT EXISTS idx_observations_status ON observations(status);
CREATE INDEX IF NOT EXISTS idx_states_entity ON states(entity_type, entity_id, start_at);
CREATE INDEX IF NOT EXISTS idx_state_transitions_state ON state_transitions(state_id);
CREATE INDEX IF NOT EXISTS idx_change_points_metric ON change_points(metric_id, effective_at);
CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status);
CREATE INDEX IF NOT EXISTS idx_pattern_instances_pattern ON pattern_instances(pattern_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_owner ON evidence_links(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_target ON evidence_links(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_commits_seq ON commits(seq);
CREATE INDEX IF NOT EXISTS idx_changes_commit ON changes(commit_id);
CREATE INDEX IF NOT EXISTS idx_changes_entity ON changes(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_versions_entity ON entity_versions(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_legacy_id_map_new ON legacy_id_map(new_id);
CREATE INDEX IF NOT EXISTS idx_fsck_findings_run ON fsck_findings(run_id);
