from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .db import connect
from .manifest import load_manifest
from .ontology_spec import DEFAULT_ONTOLOGY_PATH
from .registries import get_or_register_model
from .rustcore import rust_binary_path

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"
_RUST_CARGO_TOML = "fieldhorizon-rust/Cargo.toml"


def _path_fingerprint(paths: list[Path]) -> str | None:
    """
    Cheap, content-free fingerprint: sha256 of name:mtime_ns:size for each
    existing path, sorted for stability. The exact scheme
    rustcore.json_corpus_fingerprint already uses -- reused here, not
    reinvented. Answers "did this change since," not "what was it" (see
    the Phase B dossier's L2-replay discussion for why that's enough).
    """
    entries = []
    for path in sorted(paths, key=str):
        if not path.exists():
            continue
        stat = path.stat()
        entries.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")

    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def ontology_fingerprint() -> str | None:
    return _path_fingerprint([DEFAULT_ONTOLOGY_PATH])


def source_manifest_fingerprint(cfg: AppConfig) -> str | None:
    """sources.yaml itself, plus every local file path it lists."""
    manifest_file = cfg.root / "data" / "sources.yaml"
    paths = [manifest_file]

    if manifest_file.exists():
        try:
            entries = load_manifest(manifest_file)
            paths.extend(entry.resolve_path(cfg) for entry in entries)
        except Exception:
            pass  # malformed manifest -- fingerprint just the file itself, not a load_config-time concern here

    return _path_fingerprint(paths)


def configuration_fingerprint(cfg: AppConfig) -> str | None:
    return _path_fingerprint([cfg.root / "config.yaml"])


def dependency_fingerprint() -> str | None:
    """
    Hash of pyproject.toml's own content -- the only current, reliable
    dependency-pin source. No lock file exists in this repo, and
    requirements.txt is stale (loose >= pins, missing several dependencies
    added across later phases) -- regenerating it is a separate concern
    this phase doesn't silently absorb.
    """
    if not _PYPROJECT_PATH.exists():
        return None
    return hashlib.sha256(_PYPROJECT_PATH.read_bytes()).hexdigest()


def rust_binary_fingerprint(cfg: AppConfig) -> str | None:
    """Cargo.toml's version string + mtime/size of whichever binary rust_binary_path resolves to, if any."""
    cargo_toml = cfg.root / _RUST_CARGO_TOML
    version = None
    if cargo_toml.exists():
        for line in cargo_toml.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                version = stripped.split("=", 1)[1].strip().strip('"')
                break

    binary_path = rust_binary_path(cfg)
    if not binary_path.exists():
        return f"version={version}" if version else None

    stat = binary_path.stat()
    return f"version={version}:{stat.st_mtime_ns}:{stat.st_size}"


def code_commit(cfg: AppConfig) -> str:
    """git rev-parse --short HEAD, or 'unknown' if not a git repo or git is unavailable -- never raises."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cfg.root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def db_schema_version(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    operation: str
    input_fingerprint: str | None = None
    source_manifest_fingerprint: str | None = None
    ontology_fingerprint: str | None = None
    db_schema_version: int | None = None
    code_commit: str | None = None
    dependency_fingerprint: str | None = None
    rust_binary_fingerprint: str | None = None
    model_registry_ids: list | None = None
    prompt_registry_ids: list | None = None
    embedding_model_id: str | None = None
    random_seeds: dict | None = None
    configuration_fingerprint: str | None = None
    selected_evidence_ids: list | None = None
    output_ids: list | None = None
    id: int | None = None
    recorded_at: str | None = None


def _row_to_manifest(row) -> RunManifest:
    return RunManifest(
        id=int(row["id"]),
        run_id=row["run_id"],
        operation=row["operation"],
        input_fingerprint=row["input_fingerprint"],
        source_manifest_fingerprint=row["source_manifest_fingerprint"],
        ontology_fingerprint=row["ontology_fingerprint"],
        db_schema_version=row["db_schema_version"],
        code_commit=row["code_commit"],
        dependency_fingerprint=row["dependency_fingerprint"],
        rust_binary_fingerprint=row["rust_binary_fingerprint"],
        model_registry_ids=json.loads(row["model_registry_ids"]) if row["model_registry_ids"] else None,
        prompt_registry_ids=json.loads(row["prompt_registry_ids"]) if row["prompt_registry_ids"] else None,
        embedding_model_id=row["embedding_model_id"],
        random_seeds=json.loads(row["random_seeds"]) if row["random_seeds"] else None,
        configuration_fingerprint=row["configuration_fingerprint"],
        selected_evidence_ids=json.loads(row["selected_evidence_ids"]) if row["selected_evidence_ids"] else None,
        output_ids=json.loads(row["output_ids"]) if row["output_ids"] else None,
        recorded_at=row["recorded_at"],
    )


class RunManifestRepository:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def record(self, manifest: RunManifest) -> RunManifest:
        with connect(self.cfg.database) as conn:
            conn.execute(
                """
                INSERT INTO run_manifests(
                    run_id, operation, input_fingerprint, source_manifest_fingerprint,
                    ontology_fingerprint, db_schema_version, code_commit, dependency_fingerprint,
                    rust_binary_fingerprint, model_registry_ids, prompt_registry_ids,
                    embedding_model_id, random_seeds,
                    configuration_fingerprint, selected_evidence_ids, output_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.run_id, manifest.operation, manifest.input_fingerprint,
                    manifest.source_manifest_fingerprint, manifest.ontology_fingerprint,
                    manifest.db_schema_version, manifest.code_commit, manifest.dependency_fingerprint,
                    manifest.rust_binary_fingerprint,
                    json.dumps(manifest.model_registry_ids) if manifest.model_registry_ids is not None else None,
                    json.dumps(manifest.prompt_registry_ids) if manifest.prompt_registry_ids is not None else None,
                    manifest.embedding_model_id,
                    json.dumps(manifest.random_seeds) if manifest.random_seeds is not None else None,
                    manifest.configuration_fingerprint,
                    json.dumps(manifest.selected_evidence_ids) if manifest.selected_evidence_ids is not None else None,
                    json.dumps(manifest.output_ids) if manifest.output_ids is not None else None,
                ),
            )
            conn.commit()
        return manifest

    def get(self, run_id: str) -> RunManifest | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute("SELECT * FROM run_manifests WHERE run_id = ?", (run_id,)).fetchone()
        return _row_to_manifest(row) if row is not None else None

    def recent(self, operation: str | None = None, limit: int = 50) -> list[RunManifest]:
        query = "SELECT * FROM run_manifests"
        params: tuple = ()
        if operation:
            query += " WHERE operation = ?"
            params = (operation,)
        query += " ORDER BY id DESC LIMIT ?"
        params = (*params, limit)

        with connect(self.cfg.database) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_manifest(row) for row in rows]


def find_manifest_for_cycle(cfg: AppConfig, cycle_id: int) -> RunManifest | None:
    """
    RunManifest isn't indexed by cycle_id -- cycle.py/multicycle.py record
    output_ids=[cycle_id] under the operation's own run_id (== that
    cycle's Phase A correlation_id). Scans cycle-operation manifests for
    the one whose output_ids contains this id; returns None for cycles
    predating Phase B or whose manifest recording failed (best-effort, see
    record_operation_manifest).
    """
    for manifest in RunManifestRepository(cfg).recent(operation="cycle", limit=10_000):
        if manifest.output_ids and cycle_id in manifest.output_ids:
            return manifest
    return None


def build_manifest_fingerprints(cfg: AppConfig) -> dict:
    """The five path/content-based fingerprints, computed together since callers need all of them at once."""
    return {
        "source_manifest_fingerprint": source_manifest_fingerprint(cfg),
        "ontology_fingerprint": ontology_fingerprint(),
        "configuration_fingerprint": configuration_fingerprint(cfg),
        "dependency_fingerprint": dependency_fingerprint(),
        "rust_binary_fingerprint": rust_binary_fingerprint(cfg),
        "code_commit": code_commit(cfg),
        "db_schema_version": db_schema_version(cfg),
    }


def record_operation_manifest(
    cfg: AppConfig,
    run_id: str,
    operation: str,
    model: str | None = None,
    embedding_model_id: str | None = None,
    input_fingerprint: str | None = None,
    random_seeds: dict | None = None,
    selected_evidence_ids: list | None = None,
    output_ids: list | None = None,
) -> RunManifest:
    """
    Convenience wrapper bundling the path-based fingerprints with model
    registration and a single RunManifestRepository.record call -- the
    shape cycle.py/council.py/dream.py all need once at the end of their
    own operation. `run_id` is the SAME value as that operation's Phase A
    correlation_id, not a third identifier.
    """
    model_registry_ids = [get_or_register_model(cfg, model)] if model else None
    fingerprints = build_manifest_fingerprints(cfg)

    manifest = RunManifest(
        run_id=run_id,
        operation=operation,
        input_fingerprint=input_fingerprint,
        source_manifest_fingerprint=fingerprints["source_manifest_fingerprint"],
        ontology_fingerprint=fingerprints["ontology_fingerprint"],
        db_schema_version=fingerprints["db_schema_version"],
        code_commit=fingerprints["code_commit"],
        dependency_fingerprint=fingerprints["dependency_fingerprint"],
        rust_binary_fingerprint=fingerprints["rust_binary_fingerprint"],
        model_registry_ids=model_registry_ids,
        embedding_model_id=embedding_model_id,
        random_seeds=random_seeds,
        configuration_fingerprint=fingerprints["configuration_fingerprint"],
        selected_evidence_ids=selected_evidence_ids,
        output_ids=output_ids,
    )
    return RunManifestRepository(cfg).record(manifest)
