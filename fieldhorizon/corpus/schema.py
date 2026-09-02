"""
Harvester tables, added to the existing Field Horizon database.

This follows db.py's own convention exactly -- one `CREATE TABLE IF NOT
EXISTS` block per table, additive only, safe to re-run -- rather than
introducing a migration framework the repository has never had. Nothing
here alters or drops a pre-existing table; the harvester's only contact
with the original schema is that it *writes rows into* `sources` and
`chunks` at ingestion time, using the same shape `_ingest_manifest_entry`
already writes.
"""

from __future__ import annotations

import sqlite3

CORPUS_SCHEMA = """
-- One row per configured acquisition source (Standard Ebooks, Gutenberg,
-- ...). Rows are upserted from config/corpus_sources.yaml on every run, so
-- the YAML stays the source of truth for configuration while this table
-- carries the *operational* state (last successful harvest, cursor) that
-- must survive a restart.
CREATE TABLE IF NOT EXISTS corpus_sources (
    source_id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    base_url TEXT,
    trust REAL NOT NULL DEFAULT 0.5,
    -- Opaque, adapter-defined resumption cursor (an OAI-PMH resumption
    -- token, an OPDS next-page URL, a Gutenberg catalogue row offset).
    -- The orchestrator never interprets it.
    cursor TEXT,
    last_discovery_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- One row per (source, run) discovery pass. Keeps per-source outcomes
-- separable when one source fails and the others succeed -- the brief's
-- "si une source distante est indisponible, ne pas bloquer tout le projet".
CREATE TABLE IF NOT EXISTS corpus_source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    discovered INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    cursor_before TEXT,
    cursor_after TEXT
);

CREATE INDEX IF NOT EXISTS idx_corpus_source_runs_run ON corpus_source_runs(run_id);

-- A discovered catalogue record, before acquisition. Separate from
-- corpus_items so that discovery can be exhaustive (the brief: "la
-- découverte de métadonnées peut être exhaustive") while acquisition
-- stays budgeted -- millions of candidates may exist for a handful of
-- items. UNIQUE(source_id, external_id) makes re-discovery idempotent.
CREATE TABLE IF NOT EXISTS corpus_candidates (
    candidate_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    authors TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT '',
    subjects TEXT NOT NULL DEFAULT '[]',
    canonical_url TEXT NOT NULL DEFAULT '',
    download_url TEXT NOT NULL DEFAULT '',
    download_format TEXT NOT NULL DEFAULT '',
    estimated_bytes INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'DISCOVERED',
    selection_score REAL,
    selection_rationale TEXT,
    -- The adapter's faithful report of what the provider said about
    -- rights, verbatim -- never the verdict, which lives in
    -- corpus_rights_decisions.
    rights_signal_json TEXT NOT NULL DEFAULT '{}',
    raw_metadata_json TEXT NOT NULL DEFAULT '{}',
    raw_metadata_sha256 TEXT,
    discovered_at TEXT NOT NULL,
    discovered_run_id TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_corpus_candidates_state ON corpus_candidates(state);
CREATE INDEX IF NOT EXISTS idx_corpus_candidates_source ON corpus_candidates(source_id);
CREATE INDEX IF NOT EXISTS idx_corpus_candidates_score ON corpus_candidates(selection_score DESC);

-- An acquired document. document_id is a stable content-independent
-- identifier minted at acquisition time (see storage.mint_document_id) so
-- that re-running the pipeline against the same catalogue record always
-- addresses the same row -- re-download and re-normalization must never
-- fork a document's identity.
--
-- parent_document_id models the brief's composite-document case: a
-- collection of pamphlets becomes one parent plus N children, each
-- classified separately. NULL for the overwhelmingly common single-work
-- document.
CREATE TABLE IF NOT EXISTS corpus_items (
    document_id TEXT PRIMARY KEY,
    candidate_id TEXT,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    parent_document_id TEXT REFERENCES corpus_items(document_id) ON DELETE SET NULL,
    canonical_work_id TEXT,
    edition_id TEXT,

    title TEXT NOT NULL DEFAULT '',
    authors TEXT NOT NULL DEFAULT '[]',
    contributors TEXT NOT NULL DEFAULT '[]',
    translator TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    publication_date TEXT,
    edition_date TEXT,
    document_type TEXT NOT NULL DEFAULT '',
    subjects TEXT NOT NULL DEFAULT '[]',

    canonical_url TEXT NOT NULL DEFAULT '',
    content_url TEXT NOT NULL DEFAULT '',
    source_adapter TEXT NOT NULL DEFAULT '',
    discovered_at TEXT,
    downloaded_at TEXT,

    source_format TEXT NOT NULL DEFAULT '',
    -- The MIME actually sniffed from the bytes, which is not necessarily
    -- what the server's Content-Type header claimed (security rule:
    -- "détection MIME réelle").
    detected_mime TEXT NOT NULL DEFAULT '',
    declared_mime TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    raw_sha256 TEXT,
    normalized_sha256 TEXT,

    normalized_chars INTEGER NOT NULL DEFAULT 0,
    quality_score REAL,
    quality_report_json TEXT NOT NULL DEFAULT '{}',
    duplicate_score REAL,
    duplicate_of TEXT REFERENCES corpus_items(document_id) ON DELETE SET NULL,
    simhash TEXT,

    destination TEXT,
    manifesto_score REAL,
    classification_confidence REAL,
    classification_low_confidence INTEGER NOT NULL DEFAULT 0,
    classifier_version TEXT,
    classifier_kind TEXT,

    rights_status TEXT NOT NULL DEFAULT 'RIGHTS_PENDING',
    normalized_license TEXT,
    license_original_text TEXT,
    license_evidence_url TEXT,
    license_evidence_sha256 TEXT,
    rights_verified_at TEXT,
    jurisdictions TEXT NOT NULL DEFAULT '[]',
    distribution_scope TEXT NOT NULL DEFAULT 'UNKNOWN',
    attribution_required INTEGER NOT NULL DEFAULT 0,
    share_alike INTEGER NOT NULL DEFAULT 0,

    state TEXT NOT NULL DEFAULT 'DISCOVERED',
    pipeline_version TEXT,
    normalizer_version TEXT,
    chunker_version TEXT,
    -- Always 1. Present as a column rather than as an assumption so that
    -- anything reading corpus_items -- including a future consumer that
    -- never read this docstring -- is told explicitly that the text is
    -- third-party data and never an instruction (rules 12-14).
    untrusted_content INTEGER NOT NULL DEFAULT 1,

    materialized_path TEXT,
    source_row_id INTEGER,
    ingested_at TEXT,
    indexed_at TEXT,

    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_corpus_items_state ON corpus_items(state);
CREATE INDEX IF NOT EXISTS idx_corpus_items_raw_sha ON corpus_items(raw_sha256);
CREATE INDEX IF NOT EXISTS idx_corpus_items_norm_sha ON corpus_items(normalized_sha256);
CREATE INDEX IF NOT EXISTS idx_corpus_items_work ON corpus_items(canonical_work_id);
CREATE INDEX IF NOT EXISTS idx_corpus_items_destination ON corpus_items(destination);

-- Every byte-level artifact a document ever had: the immutable raw
-- download, the normalized text, and the stored licence proof. Content-
-- addressed (sha256 is the blob's name on disk), so two documents that
-- download to identical bytes share one blob.
CREATE TABLE IF NOT EXISTS corpus_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES corpus_items(document_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mime TEXT,
    encoding TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id, kind, sha256)
);

CREATE INDEX IF NOT EXISTS idx_corpus_artifacts_doc ON corpus_artifacts(document_id);

-- Append-only. A document's rights history is never overwritten: an audit
-- that downgrades an accepted document writes a NEW row, and the old one
-- remains as the record of what was believed, and on what evidence, at
-- the time it was indexed. Same discipline as domain_events /
-- provenance_edges / canon_temporal_states elsewhere in this schema.
CREATE TABLE IF NOT EXISTS corpus_rights_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    candidate_id TEXT,
    decision TEXT NOT NULL,
    normalized_license TEXT NOT NULL DEFAULT '',
    rights_scope TEXT NOT NULL DEFAULT 'UNKNOWN',
    commercial_use INTEGER NOT NULL DEFAULT 0,
    redistribution INTEGER NOT NULL DEFAULT 0,
    derivatives INTEGER NOT NULL DEFAULT 0,
    attribution_required INTEGER NOT NULL DEFAULT 0,
    share_alike INTEGER NOT NULL DEFAULT 0,
    jurisdictions TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    reason_codes TEXT NOT NULL DEFAULT '[]',
    policy_profile TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    evidence_checked_at TEXT,
    notes TEXT,
    run_id TEXT,
    decided_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_corpus_rights_doc ON corpus_rights_decisions(document_id);
CREATE INDEX IF NOT EXISTS idx_corpus_rights_candidate ON corpus_rights_decisions(candidate_id);

-- Append-only classification history. A reclassify run adds a row rather
-- than editing one, so "why is this in manifesto/" always has an answer
-- even after the classifier version changes.
CREATE TABLE IF NOT EXISTS corpus_classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES corpus_items(document_id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    manifesto_score REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.0,
    document_form TEXT,
    primary_domain TEXT,
    secondary_tags TEXT NOT NULL DEFAULT '[]',
    normative_intent REAL,
    mobilization_intent REAL,
    doctrinal_intent REAL,
    narrative_intent REAL,
    analytical_intent REAL,
    rationale TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    classifier_version TEXT NOT NULL DEFAULT '',
    classifier_kind TEXT NOT NULL DEFAULT '',
    low_confidence INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    classified_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_corpus_classifications_doc ON corpus_classifications(document_id);

-- Groups documents the deduplicator believes are the same thing. The
-- cluster's `kind` records WHICH level of the dedup ladder matched
-- (exact_raw / exact_normalized / identifier / title_author / near), so a
-- near-duplicate cluster can be reviewed differently from a byte-identical
-- one. representative_document_id is the canonical materialization; the
-- others keep their provenance rows but are not indexed separately.
CREATE TABLE IF NOT EXISTS corpus_duplicate_clusters (
    cluster_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    representative_document_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS corpus_duplicate_members (
    cluster_id TEXT NOT NULL REFERENCES corpus_duplicate_clusters(cluster_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES corpus_items(document_id) ON DELETE CASCADE,
    similarity REAL NOT NULL DEFAULT 1.0,
    is_representative INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cluster_id, document_id)
);

-- Every state transition, append-only. This is the resumability record:
-- an interrupted run leaves a trail that says exactly which step each
-- document reached and why it stopped.
CREATE TABLE IF NOT EXISTS corpus_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    candidate_id TEXT,
    old_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    reason_codes TEXT NOT NULL DEFAULT '[]',
    pipeline_version TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    error_json TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corpus_state_events_doc ON corpus_state_events(document_id);
CREATE INDEX IF NOT EXISTS idx_corpus_state_events_run ON corpus_state_events(run_id);

-- The attribution obligations a licence imposes, kept separately from the
-- licence itself: CC BY-SA on a translated text can owe attribution to the
-- author, the translator, AND the transcriber, and flattening that into
-- one string loses obligations the export must honour.
CREATE TABLE IF NOT EXISTS corpus_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES corpus_items(document_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT,
    note TEXT,
    UNIQUE(document_id, role, name)
);

-- Structured failures. Distinguishing retryable from final is what lets
-- `corpus resume` know the difference between "the server was down" and
-- "this file is not a text document".
CREATE TABLE IF NOT EXISTS corpus_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    candidate_id TEXT,
    source_id TEXT,
    run_id TEXT,
    stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT '',
    retryable INTEGER NOT NULL DEFAULT 1,
    attempt INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_corpus_errors_run ON corpus_errors(run_id);
CREATE INDEX IF NOT EXISTS idx_corpus_errors_doc ON corpus_errors(document_id);

-- One row per harvester run, so `corpus status` and `corpus resume` can
-- answer "what happened last time" without replaying the event log.
CREATE TABLE IF NOT EXISTS corpus_runs (
    run_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    rights_profile TEXT NOT NULL,
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats_json TEXT NOT NULL DEFAULT '{}',
    plan_path TEXT,
    report_path TEXT,
    pipeline_version TEXT,
    error_message TEXT
);

-- The HTTP conditional-request cache: ETag / Last-Modified per URL, so a
-- re-harvest of an unchanged catalogue costs a 304 rather than a full
-- re-download (brief: ETag/Last-Modified handling, and idempotence).
-- ---------------------------------------------------------------------
-- Phase II: the acquisition graph and the rights lattice
-- ---------------------------------------------------------------------

-- What each provider actually does, as the adapter declares it. Written
-- on every run so a capability change (a credential appearing, a
-- provider starting to refuse us) is visible as data rather than only in
-- a log line.
CREATE TABLE IF NOT EXISTS corpus_source_capabilities (
    provider_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    credentialled_capabilities TEXT NOT NULL DEFAULT '[]',
    credential_env TEXT,
    credentials_present INTEGER NOT NULL DEFAULT 0,
    rights_trust REAL NOT NULL DEFAULT 0.5,
    content_hosts TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'READY',
    notes TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Who filled which role for one document. This is the provenance record
-- Phase I could not express: after a brokered acquisition,
-- `discovered_by` might be europeana while `content_hosted_by` is
-- gallica -- two different institutions, where Phase I had one field and
-- therefore had to pretend the answer was the same.
CREATE TABLE IF NOT EXISTS corpus_acquisition_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_key TEXT NOT NULL,
    document_id TEXT,
    candidate_id TEXT,
    discovered_by TEXT,
    metadata_from TEXT,
    rights_evidence_from TEXT,
    content_hosted_by TEXT,
    fallback_hosts TEXT NOT NULL DEFAULT '[]',
    expected_format TEXT,
    normalization_pipeline TEXT,
    credential_requirement TEXT,
    provider_trust REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'METADATA_ONLY',
    download_status TEXT,
    content_url TEXT,
    reason TEXT,
    run_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_key)
);

CREATE INDEX IF NOT EXISTS idx_corpus_plans_status ON corpus_acquisition_plans(status);
CREATE INDEX IF NOT EXISTS idx_corpus_plans_document ON corpus_acquisition_plans(document_id);

-- One row per rights COMPONENT per document, plus one summary row for
-- the intersected result. Append-only like corpus_rights_decisions: an
-- audit that re-evaluates a stack writes new rows, so the record of what
-- each layer said at index time survives.
--
-- This is the table that makes "why is this LOCAL_US_ONLY" answerable:
-- the limiting component is named rather than inferred from a licence
-- string that lost the distinction.
CREATE TABLE IF NOT EXISTS corpus_rights_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    candidate_id TEXT,
    component TEXT NOT NULL,
    raw_license TEXT,
    rights_statement_uri TEXT,
    normalized_license TEXT NOT NULL DEFAULT 'unknown',
    not_applicable INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    run_id TEXT,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_corpus_rights_components_doc
    ON corpus_rights_components(document_id);

-- The intersected outcome, with the component that produced the
-- narrowest answer named explicitly.
CREATE TABLE IF NOT EXISTS corpus_rights_lattice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    candidate_id TEXT,
    effective_license TEXT NOT NULL DEFAULT 'unknown',
    scope TEXT NOT NULL DEFAULT 'UNKNOWN',
    commercial_use INTEGER NOT NULL DEFAULT 0,
    redistribution INTEGER NOT NULL DEFAULT 0,
    derivatives INTEGER NOT NULL DEFAULT 0,
    attribution_required INTEGER NOT NULL DEFAULT 0,
    share_alike INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    limiting_components TEXT NOT NULL DEFAULT '[]',
    reason_codes TEXT NOT NULL DEFAULT '[]',
    component_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    run_id TEXT,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_corpus_rights_lattice_doc ON corpus_rights_lattice(document_id);

-- Bulk-snapshot progress. A multi-gigabyte dump is fetched across
-- several bounded runs, so the byte offset, the checkpoint, and the
-- checksum all have to survive a restart -- otherwise "resumable" means
-- "starts again", which for a 40 GB file is not the same thing at all.
CREATE TABLE IF NOT EXISTS corpus_dump_state (
    dump_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    dump_url TEXT NOT NULL,
    dump_date TEXT,
    expected_sha1 TEXT,
    expected_bytes INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    part_path TEXT,
    final_path TEXT,
    checksum_verified INTEGER NOT NULL DEFAULT 0,
    -- Opaque resumption point inside the decompressed stream: for a
    -- multistream dump this is a byte offset plus the last page id
    -- processed, so a restart neither repeats nor skips.
    checkpoint TEXT,
    pages_seen INTEGER NOT NULL DEFAULT 0,
    pages_imported INTEGER NOT NULL DEFAULT 0,
    last_revision_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    started_at TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS corpus_http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    fetched_at TEXT,
    status_code INTEGER,
    content_sha256 TEXT
);
"""


def init_corpus_schema(conn: sqlite3.Connection) -> None:
    """
    Create the harvester tables. Idempotent and additive: every statement
    is IF NOT EXISTS, so calling this on a database that already has them
    is a no-op, and calling it on one that has none of them is a full
    install. Invoked from db.init_db(), so `field-horizon init` and every
    command that calls init_db() migrates automatically.
    """
    conn.executescript(CORPUS_SCHEMA)
