from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

from .config import AppConfig
from .ontology_spec import DEFAULT_ONTOLOGY_PATH

logger = logging.getLogger(__name__)


def rust_binary_path(cfg: AppConfig) -> Path:
    rust_dir = cfg.root / "fieldhorizon-rust"
    release = rust_dir / "target" / "release" / "fh-core"
    if release.exists():
        return release
    return rust_dir / "target" / "debug" / "fh-core"


def run_rust_pressure_core(
    cfg: AppConfig,
    input_path: Path,
    limit: int = 50,
) -> dict:
    binary = rust_binary_path(cfg)

    if not binary.exists():
        raise FileNotFoundError(
            f"Rust core binary not found: {binary}. "
            "Run: cd fieldhorizon-rust && cargo build --release"
        )

    cmd = [
        str(binary),
        "--input",
        str(input_path),
        "--limit",
        str(limit),
        "--ontology",
        str(DEFAULT_ONTOLOGY_PATH),
    ]

    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )

    return json.loads(result.stdout)


def export_rust_pressure_report(
    cfg: AppConfig,
    input_path: Path,
    limit: int = 50,
) -> Path:
    report = run_rust_pressure_core(cfg, input_path=input_path, limit=limit)

    out_dir = cfg.outputs / "rust_core"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "pressure_report.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return out_path

def merge_json_corpus(cfg: AppConfig) -> Path:
    merged: list[dict] = []

    for path in sorted(cfg.json_corpus.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

        if isinstance(data, list):
            merged.extend(data)
        elif isinstance(data, dict):
            merged.append(data)
        else:
            raise ValueError(f"Unsupported JSON structure in {path}: expected list or object")

    out_dir = cfg.outputs / "rust_core"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "merged_json_corpus.json"
    out_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return out_path


def json_corpus_fingerprint(cfg: AppConfig) -> str:
    """
    A hash of (filename, mtime, size) for every file in json_corpus, plus
    ontology.yaml itself (its oppositions feed directly into the pressure
    report, so an edit there must invalidate the cache too). Cheap to
    compute -- no file content is read.
    """
    entries = []

    for path in sorted(cfg.json_corpus.glob("*.json")):
        stat = path.stat()
        entries.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")

    if DEFAULT_ONTOLOGY_PATH.exists():
        stat = DEFAULT_ONTOLOGY_PATH.stat()
        entries.append(f"{DEFAULT_ONTOLOGY_PATH.name}:{stat.st_mtime_ns}:{stat.st_size}")

    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def export_rust_pressure_report_all(
    cfg: AppConfig,
    limit: int = 100,
) -> Path:
    """
    Cached: re-merging the corpus and re-running the Rust binary on every
    multi-cycle was pure waste when nothing in json_corpus had changed.
    Regenerates only when the corpus fingerprint or `limit` changes.
    """
    out_dir = cfg.outputs / "rust_core"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "pressure_report.json"
    cache_meta_path = out_dir / "pressure_report_cache.json"

    cache_key = {"fingerprint": json_corpus_fingerprint(cfg), "limit": limit}

    if report_path.exists() and cache_meta_path.exists():
        try:
            cached_key = json.loads(cache_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached_key = None

        if cached_key == cache_key:
            logger.debug("Rust pressure report cache hit; skipping regeneration.")
            return report_path

    merged_path = merge_json_corpus(cfg)
    result_path = export_rust_pressure_report(
        cfg,
        input_path=merged_path,
        limit=limit,
    )
    cache_meta_path.write_text(json.dumps(cache_key), encoding="utf-8")
    return result_path

def rust_report_to_prompt(report: dict, limit: int = 12) -> str:
    edges = report.get("edges", [])[:limit]

    if not edges:
        return "[no rust pressure edges detected]"

    lines = [
        f"RUST_CORE_NODES: {report.get('node_count', 0)}",
        f"RUST_CORE_EDGES: {report.get('edge_count', 0)}",
        "",
        "ACTIVE_RUST_PRESSURE_GRAPH:",
    ]

    for edge in edges:
        lines.append(
            f"- {edge['source_id']} / {edge['source_category']} "
            f"→ {edge['target_id']} / {edge['target_category']} "
            f"| score={edge['score']} | {edge['reason']}"
        )

    return "\n".join(lines)

def filter_rust_report_by_refs(report: dict, refs: list[str]) -> dict:
    wanted = set(refs)
    edges = report.get("edges", [])

    filtered_edges = [
        edge for edge in edges
        if edge.get("source_id") in wanted
        or edge.get("target_id") in wanted
    ]

    return {
        "node_count": report.get("node_count", 0),
        "edge_count": len(filtered_edges),
        "edges": filtered_edges,
    }