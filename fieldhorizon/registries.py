from __future__ import annotations

import hashlib
import logging

import requests

from .config import AppConfig
from .db import connect

logger = logging.getLogger(__name__)

_model_id_cache: dict[tuple[str, str], str] = {}


class EmbeddingVersionMismatch(ValueError):
    """
    Raised only by assert_same_embedding_version -- a narrow, explicit
    guard for NEW call sites that manipulate raw vectors directly. Every
    EXISTING comparison site (storage_vectors.py, weather.py) keeps its
    current behavior of silently filtering out a stale-version row
    (indistinguishable from "not embedded yet," which is the correct,
    deliberately chosen behavior during a backfill) -- this class does not
    change that, by design (see the Phase B dossier, open question 2).
    """


def assert_same_embedding_version(a: str, b: str) -> None:
    if a != b:
        raise EmbeddingVersionMismatch(f"vectors from different embedding versions are never comparable: {a!r} != {b!r}")


def _resolve_model_digest(cfg: AppConfig, model_name: str) -> str:
    """
    Same /api/tags digest-resolution pattern as
    embeddings.current_embedding_version, kept as its own function (not a
    shared import) so this new registry path can never regress that
    already-tested, carefully-tuned caching/fallback logic -- a small,
    deliberate duplication over a risky refactor of working code.
    """
    cache_key = (cfg.ollama_base_url, model_name)
    if cache_key in _model_id_cache:
        return _model_id_cache[cache_key]

    model_id = model_name

    try:
        url = cfg.ollama_base_url.rstrip("/") + "/api/tags"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        for entry in res.json().get("models", []):
            name = str(entry.get("name", ""))
            if name == model_name or name.split(":")[0] == model_name:
                digest = str(entry.get("digest", ""))
                if digest:
                    model_id = f"{model_name}@{digest[:12]}"
                break
    except Exception as exc:
        logger.warning("Could not resolve model digest for %s, using bare model name: %s", model_name, exc)

    _model_id_cache[cache_key] = model_id
    return model_id


def get_or_register_model(cfg: AppConfig, model_name: str) -> str:
    """
    Resolves model_name to a model_id (the same "name@digest[:12]"
    convention current_embedding_version already uses) and registers a
    model_registry row on first sight -- an unknown model is never used
    silently. Returns the model_id every caller should record alongside
    its own operation (e.g. in a RunManifest).
    """
    model_id = _resolve_model_digest(cfg, model_name)

    with connect(cfg.database) as conn:
        existing = conn.execute("SELECT model_id FROM model_registry WHERE model_id = ?", (model_id,)).fetchone()
        if existing is None:
            digest = model_id.split("@", 1)[1] if "@" in model_id else None
            conn.execute(
                "INSERT INTO model_registry(model_id, name, digest) VALUES (?, ?, ?)",
                (model_id, model_name, digest),
            )
            conn.commit()

    return model_id


def get_or_register_prompt(cfg: AppConfig, role: str, template_text: str) -> str:
    """
    prompt_id is a hash of the template text itself, auto-registered like
    a model -- this system's prompts are Python string templates, not
    externally versioned files, so there is no human-maintained version
    number to record instead.
    """
    prompt_id = hashlib.sha256(template_text.encode("utf-8")).hexdigest()[:16]

    with connect(cfg.database) as conn:
        existing = conn.execute("SELECT prompt_id FROM prompt_registry WHERE prompt_id = ?", (prompt_id,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO prompt_registry(prompt_id, role, template_excerpt) VALUES (?, ?, ?)",
                (prompt_id, role, template_text[:500]),
            )
            conn.commit()

    return prompt_id


def get_or_register_embedding_model(cfg: AppConfig, embedding_version: str, model_name: str, dim: int | None = None) -> str:
    """
    Formalizes the embedding_version string (already produced by
    embeddings.current_embedding_version) into a real embedding_registry
    row -- embedding_model_id IS embedding_version, not a third identifier.
    """
    with connect(cfg.database) as conn:
        existing = conn.execute(
            "SELECT embedding_model_id FROM embedding_registry WHERE embedding_model_id = ?", (embedding_version,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO embedding_registry(embedding_model_id, model_name, dim) VALUES (?, ?, ?)",
                (embedding_version, model_name, dim),
            )
            conn.commit()

    return embedding_version


_LIST_QUERIES = {
    "model": "SELECT model_id AS id, name, digest, active, first_seen_at FROM model_registry ORDER BY first_seen_at DESC",
    "prompt": "SELECT prompt_id AS id, role, active, first_seen_at FROM prompt_registry ORDER BY first_seen_at DESC",
    "embedding": "SELECT embedding_model_id AS id, model_name, dim, first_seen_at FROM embedding_registry ORDER BY first_seen_at DESC",
}

_SHOW_QUERIES = {
    "model": ("SELECT * FROM model_registry WHERE model_id = ?", "model_id"),
    "prompt": ("SELECT * FROM prompt_registry WHERE prompt_id = ?", "prompt_id"),
    "embedding": ("SELECT * FROM embedding_registry WHERE embedding_model_id = ?", "embedding_model_id"),
}


def list_registry(cfg: AppConfig, kind: str) -> list[dict]:
    """kind is one of 'model', 'prompt', 'embedding' -- the three registries this phase introduces."""
    if kind not in _LIST_QUERIES:
        raise ValueError(f"Unknown registry kind {kind!r}, expected one of {sorted(_LIST_QUERIES)}")
    with connect(cfg.database) as conn:
        rows = conn.execute(_LIST_QUERIES[kind]).fetchall()
    return [dict(row) for row in rows]


def show_registry_entry(cfg: AppConfig, kind: str, entry_id: str) -> dict | None:
    if kind not in _SHOW_QUERIES:
        raise ValueError(f"Unknown registry kind {kind!r}, expected one of {sorted(_SHOW_QUERIES)}")
    query, _ = _SHOW_QUERIES[kind]
    with connect(cfg.database) as conn:
        row = conn.execute(query, (entry_id,)).fetchone()
    return dict(row) if row is not None else None
