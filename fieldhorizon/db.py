from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    language TEXT DEFAULT 'unknown',
    -- Curation manifest fields (Civilization Engine corpus-scale phase).
    -- manifest_id is the data/sources.yaml entry id this source was
    -- ingested from -- NULL for anything ingested the old directory-scan
    -- way (ingest --books/--manifestos), which still works unchanged.
    -- ingest-manifest is idempotent by manifest_id, not by path.
    manifest_id TEXT,
    author TEXT,
    year INTEGER,
    license TEXT,
    weight REAL DEFAULT 1.0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- The idx_sources_manifest_id partial unique index is created in
-- _migrate_sources_table, not here: on a pre-existing database the
-- `manifest_id` column doesn't exist until that migration adds it, and
-- CREATE INDEX on a not-yet-existing column fails outright.

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    canonical_ref TEXT NOT NULL,
    content TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    metadata TEXT DEFAULT '{}',
    -- Exact offsets into the original source file (Civilization Engine
    -- deep-provenance phase). Nullable: computed for every new ingest,
    -- backfilled best-effort for older rows by searching the chunk's own
    -- text in the source file -- a search that can fail (content changed,
    -- encoding differences), which is why this isn't NOT NULL.
    char_start INTEGER,
    char_end INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    -- Set once concepts.tag_chunk has processed this chunk, whether or not
    -- it produced any concepts/entities/motifs rows -- an empty tagging
    -- result is a valid outcome, so "has a chunk_concepts row" can't be
    -- used as the resumability marker on its own.
    concepts_tagged_at TEXT,
    UNIQUE(source_id, chunk_index)
);

-- A real (non-contentless) FTS5 index: source_type is UNINDEXED so it can
-- still be used as an exact-match filter alongside MATCH without being
-- tokenized. Contentless (content='') tables cannot have their columns
-- read back at all (SQLite returns NULL for every column on SELECT), which
-- is why this table was never actually queried before.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    canonical_ref,
    content,
    source_title,
    source_type UNINDEXED
);

CREATE TABLE IF NOT EXISTS json_entries (
    id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    category TEXT NOT NULL,
    tradition TEXT DEFAULT 'synthetic',
    statement TEXT NOT NULL,
    gloss TEXT DEFAULT '',
    targets TEXT DEFAULT '[]',
    tone TEXT DEFAULT '',
    severity REAL DEFAULT 0.5,
    mutation_potential REAL DEFAULT 0.5,
    doctrinal_axes TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    raw_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Non-contentless for the same reason as chunks_fts: on a contentless
-- table, even `DELETE ... WHERE id = ?` silently matches nothing, so a
-- dedup-on-reingest delete looks like it works but doesn't.
CREATE VIRTUAL TABLE IF NOT EXISTS json_entries_fts USING fts5(
    id UNINDEXED,
    category,
    tradition,
    statement,
    gloss,
    tags
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    dry_run INTEGER DEFAULT 0,
    verdict TEXT,
    final_score REAL,
    fragment TEXT,
    -- Genealogy (Civilization Engine phase): parent_cycle_ids is a JSON
    -- array of the cycle ids whose canon fragments were MMR-selected into
    -- this cycle's prompt -- this cycle's doctrinal ancestry. retired_at/
    -- retirement_reason mark a once-CANON cycle a council audit later
    -- judged no longer canon-worthy; retirement never deletes a row, it
    -- only excludes it from future MMR selection (canon.py) while keeping
    -- it in lineage.
    parent_cycle_ids TEXT,
    retired_at TEXT,
    retirement_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cycle_sources (
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    content TEXT NOT NULL
);

-- Genealogical event log (Civilization Engine phase): every promotion,
-- retirement, and council verdict a cycle ever receives, append-only.
-- event is one of PROMOTED, RETIRED, REHABILITATED, COUNCIL_UPHELD,
-- COUNCIL_OVERTURNED (see council.py's module docstring for exactly when
-- each is written). detail is a free-form JSON blob (old/new verdict and
-- score, reason text) -- this table is history, not queried structurally
-- elsewhere, so it doesn't need its own typed columns per event kind.
CREATE TABLE IF NOT EXISTS canon_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- One row per `field-horizon council` invocation: when it ran, how many
-- fragments/canon entries it examined, and how many verdicts it changed
-- (rehabilitated or retired). notes holds the path to the markdown report.
CREATE TABLE IF NOT EXISTS councils (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    examined INTEGER DEFAULT 0,
    overturned INTEGER DEFAULT 0,
    notes TEXT
);

-- One embedding vector per canon fragment (review's real-upgrade tier §1),
-- computed at write time by fieldhorizon.embeddings.embed_and_store_fragment
-- and consumed by MMR canon selection (§2). vector is a packed float32
-- array (see embeddings.vector_to_blob); dim/model are stored alongside it
-- so a future switch of embedding model doesn't silently mix incompatible
-- vectors -- callers can filter on model. embedding_version (model name +
-- revision string, see embeddings.current_embedding_version) is the actual
-- comparability key enforced in code: two vectors are only ever compared
-- when their embedding_version strings match (standing rule -- vectors
-- from different model revisions must never be compared).
CREATE TABLE IF NOT EXISTS canon_embeddings (
    cycle_id INTEGER PRIMARY KEY REFERENCES cycles(id) ON DELETE CASCADE,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Distinct 3-grams per canon fragment (review's real-upgrade tier §2).
-- One row per (cycle_id, ngram) rather than a running global counter, so
-- "frequency across the last N canon fragments" is a windowed query
-- (COUNT of distinct cycle_ids per ngram, restricted to the N most recent
-- CANON cycles) instead of a counter that never forgets old fragments.
CREATE TABLE IF NOT EXISTS canon_ngrams (
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    ngram TEXT NOT NULL,
    PRIMARY KEY (cycle_id, ngram)
);

-- One embedding per json_entries row (review's real-upgrade tier §3):
-- an axiom's statement never changes between cycles, unlike a fragment's
-- sentences (new every evaluation), so it's cached here instead of
-- re-embedded on every doctrinal-enforcement scoring call.
CREATE TABLE IF NOT EXISTS axiom_embeddings (
    id TEXT PRIMARY KEY REFERENCES json_entries(id) ON DELETE CASCADE,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- One embedding per book/manifesto chunk (review §7's hybrid-retrieval
-- follow-up): embedded best-effort at ingest time and consumed by
-- retrieval.search_books_by_embedding to widen recall when FTS5 +
-- domain routing come up short, rather than falling back to arbitrary
-- early chunks by id.
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Knowledge graph as SQL joins, not a graph database (Civilization Engine
-- corpus-scale phase): one temperature-0 LLM call per chunk at ingest
-- populates these three tables. chunk_concepts.domain is validated
-- against ontology.yaml's own domain list at write time -- a hallucinated
-- domain name is rejected there, never stored, so this table can never
-- disagree with the ontology about what domains exist.
CREATE TABLE IF NOT EXISTS chunk_concepts (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    domain TEXT NOT NULL,
    confidence REAL NOT NULL,
    PRIMARY KEY (chunk_id, domain)
);

CREATE TABLE IF NOT EXISTS chunk_entities (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (chunk_id, entity, kind)
);

CREATE TABLE IF NOT EXISTS chunk_motifs (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    motif TEXT NOT NULL,
    PRIMARY KEY (chunk_id, motif)
);

-- Semantic fingerprint per source (Civilization Engine corpus-scale
-- phase): mean chunk embedding, domain distribution, and top motifs,
-- recomputed on demand by `field-horizon fingerprint`. One row per
-- (source, embedding_version) so a model change doesn't silently mix a
-- fingerprint computed under old vectors with a comparison against new
-- ones -- same comparability discipline as every other embedding table.
CREATE TABLE IF NOT EXISTS source_fingerprints (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    embedding_version TEXT NOT NULL,
    mean_vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    domain_distribution TEXT NOT NULL,
    top_motifs TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, embedding_version)
);

-- Doctrinal weather (Civilization Engine observability phase, READ-ONLY
-- analytics -- never consulted by retrieval, evaluation, or generation).
-- axis_vector = mean(embed(positive anchors)) - mean(embed(negative
-- anchors)), normalized; deterministic given ontology.yaml's anchors and
-- the embedding_version, so it is computed once and cached here rather
-- than on every reading.
CREATE TABLE IF NOT EXISTS weather_axis_vectors (
    axis TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (axis, embedding_version)
);

-- Time series: one row per (scope, axis) reading. scope is "canon"
-- (weighted mean over active, non-retired CANON cycles -- "deep weather")
-- or "surface" (recent cycles regardless of verdict). Written after every
-- cycle and every council, plus a one-time backfill across existing canon
-- keyed by each cycle's own created_at.
CREATE TABLE IF NOT EXISTS weather_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    scope TEXT NOT NULL,
    axis TEXT NOT NULL,
    value REAL NOT NULL,
    cycle_id INTEGER REFERENCES cycles(id) ON DELETE SET NULL,
    council_id INTEGER REFERENCES councils(id) ON DELETE SET NULL
);

-- Schools of thought: descriptive clusters over active canon embeddings,
-- named and summarized by one temperature-0 LLM call per cluster. Purely
-- descriptive labels -- never consulted by retrieval or evaluation.
-- previous_school_id links a re-clustering run's school to whichever
-- prior school its membership overlaps with most, giving schools a
-- lineage across runs the same way canon cycles have one across councils.
CREATE TABLE IF NOT EXISTS schools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    previous_school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
    -- Shared by every school produced within one run_schools() call (set
    -- once at the start of that call, not per-row) -- the grouping key
    -- for "the current generation of schools," since a run's own INSERTs
    -- can otherwise straddle more than one CURRENT_TIMESTAMP second if
    -- clustering + LLM naming takes a while.
    run_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS school_members (
    school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    distance REAL NOT NULL,
    PRIMARY KEY (school_id, cycle_id)
);

-- Unified operational event fabric (Implementation Brief III, Phase A):
-- one append-only stream every domain operation (cycle, council, dream
-- run) emits into identically, whether triggered from the CLI or the
-- server -- closing the gap where only the server's /cycle route ever
-- produced a structured event. This is an audit/observability layer
-- ALONGSIDE the existing operational tables (cycles, canon_events,
-- councils), never a replacement for them: the DB rows those write
-- remain the source of truth (governing rule 5); domain_events records
-- the story around them. No UPDATE/DELETE is ever issued against this
-- table -- append-only, enforced by EventRepository exposing no such
-- method, not by a DB-level trigger.
--
-- aggregate_id is TEXT, not INTEGER, and carries no FOREIGN KEY: the
-- aggregates referenced (cycles, councils, dream runs, schools) have
-- heterogeneous primary key types elsewhere in this schema (e.g.
-- json_entries.id is TEXT while cycles.id is INTEGER), so a single typed
-- FK is not possible here -- same reasoning canon_events.detail already
-- uses for its free-form JSON blob.
--
-- canon_event_id cross-references canon_events without altering that
-- table's schema or its five-event vocabulary at all (see
-- genealogy.record_canon_event): canon_events stays the sole authority
-- for "did this cycle's verdict change," domain_events adds the broader
-- story of what else happened in the same operation.
CREATE TABLE IF NOT EXISTS domain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    run_id TEXT NOT NULL,
    canon_event_id INTEGER REFERENCES canon_events(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    -- Optional PrincipalContext seam (Implementation Brief III): NULL
    -- until an operation passes a PrincipalContext, and NULL forever for
    -- events recorded before this column existed. Never enforced against
    -- -- no roles/permissions/authorization logic reads it.
    principal_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_domain_events_correlation ON domain_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_domain_events_run ON domain_events(run_id);
CREATE INDEX IF NOT EXISTS idx_domain_events_aggregate ON domain_events(aggregate_type, aggregate_id);

-- Run manifests (Implementation Brief III, Phase B): one row per
-- meaningful run (cycle/council/dream), keyed by the SAME run_id Phase A's
-- domain_events already uses as its correlation_id -- this is a second
-- view of one operation, never a competing history. Fingerprints are
-- cheap (filename:mtime_ns:size hashes, the exact scheme
-- rustcore.json_corpus_fingerprint already uses), not full content
-- snapshots -- they answer "did this change since," not "what was it,"
-- by design (see the Phase B dossier's L2-replay discussion).
CREATE TABLE IF NOT EXISTS run_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    input_fingerprint TEXT,
    source_manifest_fingerprint TEXT,
    ontology_fingerprint TEXT,
    db_schema_version INTEGER,
    code_commit TEXT,
    dependency_fingerprint TEXT,
    rust_binary_fingerprint TEXT,
    model_registry_ids TEXT,
    prompt_registry_ids TEXT,
    embedding_model_id TEXT,
    random_seeds TEXT,
    configuration_fingerprint TEXT,
    selected_evidence_ids TEXT,
    output_ids TEXT,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_run_manifests_operation ON run_manifests(operation);

-- Model registry: every model call resolves to (and, on first sight,
-- creates) exactly one row here. model_id follows the same
-- "name@digest[:12]" convention embeddings.current_embedding_version
-- already uses, so a re-pulled model under the same tag registers as a
-- new row rather than silently reusing a stale one.
CREATE TABLE IF NOT EXISTS model_registry (
    model_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    runtime TEXT NOT NULL DEFAULT 'ollama',
    digest TEXT,
    quantization TEXT,
    context_length INTEGER,
    active INTEGER DEFAULT 1,
    retired_at TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Prompt registry: prompt_id is a hash of the template text itself
-- (auto-registered, like model_id) -- this system's prompts are Python
-- string templates, not externally versioned files, so there is no
-- human-maintained version number to record instead.
CREATE TABLE IF NOT EXISTS prompt_registry (
    prompt_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    template_excerpt TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    retired_at TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Embedding registry: formalizes current_embedding_version's existing
-- digest-resolution discipline into a real table. Comparing vectors
-- across two different embedding_model_id rows is never valid (standing
-- rule) -- storage_vectors.py/weather.py already enforce this by
-- filtering (never comparing a stale-version row at all); this table
-- additionally backs a narrow, explicit assert_same_embedding_version
-- guard (registries.py) for new call sites that manipulate raw vectors
-- directly, without changing any existing filter-based call site.
CREATE TABLE IF NOT EXISTS embedding_registry (
    embedding_model_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    dim INTEGER,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Provenance graph (Implementation Brief III, Phase C): a typed edge over
-- EXISTING rows only (cycles, chunks, json_entries, schools, sources,
-- councils) -- not a graph database, not a new mirror table per object
-- type. source_type/target_type are one of the six object kinds this
-- system actually has; source_id/target_id are that row's own id/pk as
-- TEXT, following the exact "{type}:{id}" string convention
-- run_manifests.output_ids already uses for schools. relation_type is one
-- of provenance.RELATION_TYPES -- only ever a type something in this repo
-- actually writes (see provenance.py's module docstring for the exact
-- writer of each). confidence/polarity/evidence_ref/provenance_ref
-- distinguish "how sure," "which epistemic direction (if any)," "what
-- justifies this," and "which operational run wrote this" -- see
-- provenance.py for the full field-by-field rationale. valid_to always
-- NULL today; the column exists for Phase D's bitemporal canon, nothing
-- populates it yet. creation_method distinguishes edges written live
-- ('write_time') from the one-shot historical backfill ('backfill'), so a
-- re-backfill can stay idempotent without touching live-written edges.
CREATE TABLE IF NOT EXISTS provenance_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    polarity REAL,
    evidence_ref TEXT,
    provenance_ref TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    creation_method TEXT NOT NULL DEFAULT 'write_time',
    review_status TEXT NOT NULL DEFAULT 'unreviewed'
);

CREATE INDEX IF NOT EXISTS idx_provenance_edges_source ON provenance_edges(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_provenance_edges_target ON provenance_edges(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_provenance_edges_relation ON provenance_edges(relation_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provenance_edges_natural_key
    ON provenance_edges(source_type, source_id, target_type, target_id, relation_type);

-- Bitemporal canon (Implementation Brief III, Phase D): one row per
-- verdict PERIOD a cycle passes through, purely append-only -- never
-- UPDATEd, matching domain_events/provenance_edges/run_manifests'
-- existing discipline (see temporal.py's module docstring for why this
-- deliberately does NOT store valid_to/superseded_by as mutated columns
-- the way a literal reading of the brief would). cycles.verdict/
-- retired_at remain the mutable "current state" cache every existing
-- reader still uses -- this table is the derived HISTORY of how that
-- cache got to its current value, never a replacement for it.
--
-- valid_from is when this state became true in the modeled world;
-- recorded_at is when Field Horizon actually wrote the row (transaction
-- time). These are equal for every normal write -- this system never
-- backdates anything -- and diverge only for a 'backfill' row
-- reconstructing history after the fact (creation_method, same
-- vocabulary as provenance_edges.creation_method). active=0 marks a
-- retirement: the verdict itself doesn't change, only whether it still
-- counts as active canon.
CREATE TABLE IF NOT EXISTS canon_temporal_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    final_score REAL,
    active INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_event_id TEXT,
    source_edge_id INTEGER REFERENCES provenance_edges(id) ON DELETE SET NULL,
    creation_method TEXT NOT NULL DEFAULT 'write_time',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_canon_temporal_states_cycle ON canon_temporal_states(cycle_id, id);
"""

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # SQLite enforces foreign keys (and therefore ON DELETE CASCADE) only
    # per-connection; PRAGMA foreign_keys=ON in SCHEMA only ever took effect
    # for init_db's own connection, so cascades from `sources` to `chunks`
    # were silently inactive on every other connection, including ingest's.
    conn.execute("PRAGMA foreign_keys = ON")
    # Phase UI-6 item 4 (Resilience): SQLite's own default busy timeout is
    # 0 -- a second connection hitting a writer mid-transaction (the server
    # reading while a CLI ingest/dream run writes, say) used to fail
    # immediately with "database is locked" instead of waiting a moment for
    # the writer to finish, which is the realistic, recoverable case for a
    # single-machine app like this one.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Columns added after the original `cycles` table shipped. CREATE TABLE IF
# NOT EXISTS covers fresh databases; ALTER TABLE (guarded by a table_info
# check, since SQLite has no ADD COLUMN IF NOT EXISTS) migrates existing
# ones so canon can become DB-backed without a destructive reset.
CYCLES_MIGRATION_COLUMNS = {
    "verdict": "TEXT",
    "final_score": "REAL",
    "fragment": "TEXT",
    "parent_cycle_ids": "TEXT",
    "retired_at": "TEXT",
    "retirement_reason": "TEXT",
}


def _migrate_cycles_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(cycles)").fetchall()}
    for column, col_type in CYCLES_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE cycles ADD COLUMN {column} {col_type}")


def _migrate_chunks_fts(conn: sqlite3.Connection) -> None:
    """
    Older databases have a contentless (content='') chunks_fts with no
    source_type column -- unreadable and never queried (see review §5).
    Rebuild it as a real FTS5 index backfilled from `chunks`/`sources`.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks_fts)").fetchall()}
    if "source_type" in columns:
        return

    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            canonical_ref,
            content,
            source_title,
            source_type UNINDEXED
        )
        """
    )
    rows = conn.execute(
        """
        SELECT c.canonical_ref AS canonical_ref, c.content AS content,
               s.title AS source_title, s.source_type AS source_type
        FROM chunks c
        JOIN sources s ON s.id = c.source_id
        """
    ).fetchall()
    conn.executemany(
        "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, ?, ?)",
        [(r["canonical_ref"], r["content"], r["source_title"], r["source_type"]) for r in rows],
    )


def _migrate_json_entries_fts(conn: sqlite3.Connection) -> None:
    """
    Same disease as chunks_fts, different symptom: the original
    json_entries_fts is contentless (content=''), and on a contentless
    table `DELETE ... WHERE id = ?` matches zero rows -- so the
    dedup-on-reingest delete in ingest.py silently does nothing and
    every re-ingest doubles the table. Detected via the stored CREATE
    statement (PRAGMA table_info doesn't expose content=/UNINDEXED).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='json_entries_fts'"
    ).fetchone()
    if row and "content=" not in (row["sql"] or ""):
        return

    conn.execute("DROP TABLE IF EXISTS json_entries_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE json_entries_fts USING fts5(
            id UNINDEXED,
            category,
            tradition,
            statement,
            gloss,
            tags
        )
        """
    )
    rows = conn.execute(
        "SELECT id, category, tradition, statement, gloss, tags FROM json_entries"
    ).fetchall()
    conn.executemany(
        "INSERT INTO json_entries_fts(id, category, tradition, statement, gloss, tags) VALUES (?, ?, ?, ?, ?, ?)",
        [(r["id"], r["category"], r["tradition"], r["statement"], r["gloss"], r["tags"]) for r in rows],
    )


_EMBEDDING_TABLES = ("canon_embeddings", "axiom_embeddings", "chunk_embeddings")


def _migrate_embedding_version(conn: sqlite3.Connection, table: str) -> None:
    """
    embedding_version (model name + revision string) is the actual
    comparability key enforced at every embedding comparison site -- two
    vectors are only ever compared when their embedding_version strings
    match (standing rule). Existing rows predate this column; backfill
    with their own `model` value as a placeholder version until they are
    naturally re-embedded (backfill_canon_embeddings / backfill_chunk_
    embeddings / get_axiom_vector all re-embed on a version mismatch).
    `table` is always one of the three hardcoded names in
    _EMBEDDING_TABLES, never external input.
    """
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "embedding_version" in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding_version TEXT")
    conn.execute(f"UPDATE {table} SET embedding_version = model WHERE embedding_version IS NULL")


def _migrate_chunk_offsets(conn: sqlite3.Connection) -> None:
    """
    char_start/char_end (Civilization Engine deep-provenance phase) are
    nullable on fresh installs too, so ALTER TABLE ADD COLUMN without a
    NOT NULL constraint is correct here, not just a migration compromise.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "char_start" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN char_start INTEGER")
    if "char_end" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN char_end INTEGER")
    if "concepts_tagged_at" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN concepts_tagged_at TEXT")


SOURCES_MIGRATION_COLUMNS = {
    "manifest_id": "TEXT",
    "author": "TEXT",
    "year": "INTEGER",
    "license": "TEXT",
    "weight": "REAL DEFAULT 1.0",
    "notes": "TEXT",
}


def _migrate_sources_table(conn: sqlite3.Connection) -> None:
    """
    Curation-manifest columns (Civilization Engine corpus-scale phase).
    The partial unique index on manifest_id is created here, after the
    column is guaranteed to exist, not in the static SCHEMA string --
    CREATE INDEX on a column that doesn't exist yet fails outright on a
    pre-existing database.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
    for column, col_type in SOURCES_MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {col_type}")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_manifest_id "
        "ON sources(manifest_id) WHERE manifest_id IS NOT NULL"
    )


def _migrate_run_manifests_table(conn: sqlite3.Connection) -> None:
    """
    model_registry_ids/prompt_registry_ids were added to run_manifests
    shortly after the table itself shipped (Phase B step 3), before any
    real caller wrote a row -- but a database that already ran step 1's
    schema needs the same guarded ALTER TABLE any other post-hoc column
    addition in this file uses.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(run_manifests)").fetchall()}
    for column in ("model_registry_ids", "prompt_registry_ids"):
        if column not in existing:
            conn.execute(f"ALTER TABLE run_manifests ADD COLUMN {column} TEXT")


def _migrate_domain_events_table(conn: sqlite3.Connection) -> None:
    """
    principal_id (the optional PrincipalContext seam, Implementation Brief
    III): added after domain_events itself shipped, so a database that
    already ran Phase A's schema needs the same guarded ALTER TABLE any
    other post-hoc column addition in this file uses. Nullable -- rows
    written before this seam existed have no principal to backfill.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(domain_events)").fetchall()}
    if "principal_id" not in existing:
        conn.execute("ALTER TABLE domain_events ADD COLUMN principal_id TEXT")


# Bumped once per schema-affecting phase from here forward (Implementation
# Brief III, Phase B introduces this discipline) -- run_manifests.db_schema_version
# records whichever value was current at manifest-write time. Only ever
# raised, never lowered; _migrate_schema_version is a no-op once a
# database's PRAGMA user_version already meets or exceeds it, so re-running
# init_db is always safe.
CURRENT_SCHEMA_VERSION = 6


def _migrate_schema_version(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < CURRENT_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


def init_db(db_path: Path) -> None:
    # Imported here rather than at module scope: fieldhorizon.corpus.schema
    # is a leaf module with no imports of its own, but db.py is imported by
    # nearly everything, and keeping the harvester's schema out of that
    # import graph means a corpus-side syntax error can never take down
    # `field-horizon status`.
    from .corpus.schema import init_corpus_schema

    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_cycles_table(conn)
        _migrate_chunks_fts(conn)
        _migrate_json_entries_fts(conn)
        _migrate_chunk_offsets(conn)
        _migrate_sources_table(conn)
        for table in _EMBEDDING_TABLES:
            _migrate_embedding_version(conn, table)
        _migrate_run_manifests_table(conn)
        _migrate_domain_events_table(conn)
        # Autonomous Open Corpus Harvester (schema version 5): purely
        # additive corpus_* tables. No existing table is altered by this
        # call -- the harvester's contact with the original schema is that
        # it writes `sources`/`chunks` rows at ingestion time, in exactly
        # the shape _ingest_manifest_entry already writes them.
        init_corpus_schema(conn)
        _migrate_schema_version(conn)
        conn.commit()
