from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalWeights:
    """
    Hybrid retrieval v2 (Civilization Engine corpus-scale phase): each
    candidate's final score is a weighted sum of these five components.
    Weights live here, not hardcoded in retrieval.py, so tuning the mix
    doesn't require a code change -- and `--explain` can print the exact
    weight alongside each component's raw contribution.
    """
    vector_similarity: float
    bm25: float
    domain_prior: float
    source_weight: float
    severity: float


@dataclass(frozen=True)
class RetrievalPolicy:
    """
    Retrieval planner v3 (Implementation Brief III, Phase E): config-driven
    constraints a plan can enforce, each excluding candidates and
    recording why rather than silently dropping them. Only three of the
    brief's constraint list are enforced here -- max_per_source (source
    diversity: no more than this many surviving candidates share one
    source_title), min_direct_evidence_count (a canon-genealogy candidate
    needs at least this many Phase C SUPPORTS edges -- its
    supporting_evidence_count), and max_synthetic_evidence_ratio (excludes
    a canon-genealogy candidate whose Phase C synthetic_dependency_ratio
    exceeds this). contradiction_coverage, curated_source_only,
    temporal_consistency, and token_budget are NOT enforced this phase --
    named in the Phase E dossier's open questions, not silently stubbed here.
    """
    max_per_source: int
    min_direct_evidence_count: int
    max_synthetic_evidence_ratio: float


@dataclass(frozen=True)
class AppConfig:
    root: Path
    database: Path
    books: Path
    json_corpus: Path
    outputs: Path
    logs: Path
    ollama_base_url: str
    default_model: str
    temperature: float
    top_p: float
    repeat_penalty: float
    num_ctx: int
    book_fragments: int
    json_entries: int
    chunk_chars: int
    chunk_overlap: int
    tone: str
    mode: str
    manifestos: Path
    embedding_model: str
    # Defaulted (matching config.yaml's own defaults below), not required:
    # dozens of tests construct AppConfig directly without this field, and
    # hybrid retrieval v2 tuning shouldn't force every one of them to know
    # about score-weighting internals.
    retrieval_weights: RetrievalWeights = field(
        default_factory=lambda: RetrievalWeights(
            vector_similarity=0.35, bm25=0.25, domain_prior=0.20, source_weight=0.15, severity=0.05
        )
    )
    retrieval_policy: RetrievalPolicy = field(
        default_factory=lambda: RetrievalPolicy(
            max_per_source=3, min_direct_evidence_count=1, max_synthetic_evidence_ratio=0.9
        )
    )

def load_config(path: str | Path = "config.yaml") -> AppConfig:
    # `path` is resolved relative to cwd (so `--config subdir/x.yaml` still
    # works from anywhere), but every other path in AppConfig is resolved
    # relative to *this file's* parent directory, not cwd -- otherwise
    # running the CLI from outside the repo root breaks every path.
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing config file: {cfg_path}")
    root = cfg_path.parent
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    paths = raw.get("paths", {})
    ollama = raw.get("ollama", {})
    retrieval = raw.get("retrieval", {})
    fh = raw.get("field_horizon", {})
    embeddings = raw.get("embeddings", {})

    manifestos_path = paths.get("manifestos")
    if manifestos_path is None and "manifestos" in raw:
        logger.warning(
            "Top-level 'manifestos' config key is deprecated; move it under 'paths:' in %s.",
            cfg_path,
        )
        manifestos_path = raw["manifestos"]
    if manifestos_path is None:
        manifestos_path = "data/manifestos"

    hybrid_weights = retrieval.get("hybrid_weights", {})
    policy = retrieval.get("policy", {})

    return AppConfig(
        root=root,
        database=root / paths.get("database", "data/field_horizon.sqlite3"),
        books=root / paths.get("books", "data/books"),
        json_corpus=root / paths.get("json_corpus", "data/json_corpus"),
        outputs=root / paths.get("outputs", "outputs"),
        logs=root / paths.get("logs", "logs"),
        ollama_base_url=ollama.get("base_url", "http://localhost:11434"),
        default_model=ollama.get("default_model", "hermes3:8b"),
        temperature=float(ollama.get("temperature", 1.25)),
        top_p=float(ollama.get("top_p", 0.95)),
        repeat_penalty=float(ollama.get("repeat_penalty", 1.08)),
        num_ctx=int(ollama.get("num_ctx", 8192)),
        book_fragments=int(retrieval.get("book_fragments", 6)),
        json_entries=int(retrieval.get("json_entries", 8)),
        chunk_chars=int(retrieval.get("chunk_chars", 1800)),
        chunk_overlap=int(retrieval.get("chunk_overlap", 250)),
        tone=fh.get("tone", "dark, theological, anti-modern"),
        mode=fh.get("mode", "canonical_synthesis"),
        manifestos=root / manifestos_path,
        embedding_model=embeddings.get("model", "nomic-embed-text"),
        retrieval_weights=RetrievalWeights(
            vector_similarity=float(hybrid_weights.get("vector_similarity", 0.35)),
            bm25=float(hybrid_weights.get("bm25", 0.25)),
            domain_prior=float(hybrid_weights.get("domain_prior", 0.20)),
            source_weight=float(hybrid_weights.get("source_weight", 0.15)),
            severity=float(hybrid_weights.get("severity", 0.05)),
        ),
        retrieval_policy=RetrievalPolicy(
            max_per_source=int(policy.get("max_per_source", 3)),
            min_direct_evidence_count=int(policy.get("min_direct_evidence_count", 1)),
            max_synthetic_evidence_ratio=float(policy.get("max_synthetic_evidence_ratio", 0.9)),
        ),
    )
