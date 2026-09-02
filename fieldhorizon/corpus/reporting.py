"""
Run reports, attribution files, and the export guard.

Three products:

* **Per-run reports** in `outputs/corpus/runs/<run_id>.{json,md}` plus a
  structured JSONL log at `logs/corpus/<run_id>.jsonl`. The JSON is the
  machine record, the Markdown is what a human reads, and neither ever
  contains document text or a secret -- counts, hashes, licences, and
  URLs only.

* **Attribution files** (`ATTRIBUTIONS.jsonl` / `.md`). Every accepted
  document's author, title, source, licence, canonical URL, access date,
  and obligations. This is not decoration: CC BY and CC BY-SA impose
  real obligations, and an export without this file would breach them.

* **The export guard.** `export_safe` refuses to package a
  LOCAL_US_ONLY document under `release_worldwide`, with an error naming
  the offending documents. `--filter` produces the publishable subset
  instead of failing. This is the last line of defence for the rule that
  US-only public-domain material must never leave in a worldwide
  release.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import AppConfig
from .models import DistributionScope, State
from .repository import CorpusRepository, ItemRecord
from .rights import get_profile
from .selection import CorpusProfile, coverage_report
from .storage import human_bytes

logger = logging.getLogger(__name__)


class ExportBlocked(RuntimeError):
    """An export would have included material its profile forbids."""


# --------------------------------------------------------------------------
# Structured run log
# --------------------------------------------------------------------------


class RunLog:
    """
    Append-only JSONL for one run.

    Deliberately never records document text. A harvester log that
    embedded content would (a) be enormous and (b) put untrusted text
    somewhere that later gets grepped, pasted, and occasionally fed back
    into a model. Counts and identifiers only.
    """

    def __init__(self, cfg: AppConfig, run_id: str) -> None:
        self.path = cfg.logs / "corpus" / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

    def write(self, stage: str, **fields) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "run_id": self.run_id, "stage": stage, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------
# Run reports
# --------------------------------------------------------------------------


def build_run_report(cfg: AppConfig, repo: CorpusRepository, run_id: str) -> dict:
    run = repo.get_run(run_id) or {}
    stats = json.loads(run.get("stats_json") or "{}")
    source_runs = repo.source_runs_for(run_id)
    errors = repo.errors_for_run(run_id)

    items = repo.all_items()
    profile = CorpusProfile.from_items(items)

    ingested = [
        {
            "document_id": item.document_id,
            "title": item.title,
            "authors": item.authors,
            "language": item.language,
            "destination": item.destination.value if item.destination else "",
            "source": item.source_id,
            "license": item.normalized_license,
            "distribution_scope": item.distribution_scope,
            "quality_score": item.quality_score,
            "canonical_url": item.canonical_url,
            "normalized_sha256": item.normalized_sha256,
        }
        for item in items
        if item.state in (State.INGESTED, State.INDEXED)
    ]

    from .storage import CorpusStorage, free_disk_bytes

    storage = CorpusStorage(cfg.root / "data" / "corpus")

    return {
        "run_id": run_id,
        "profile": run.get("profile"),
        "rights_profile": run.get("rights_profile"),
        "mode": run.get("mode"),
        "dry_run": bool(run.get("dry_run")),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "status": run.get("status"),
        "pipeline_version": run.get("pipeline_version"),
        "stats": stats,
        "sources": [
            {
                "source_id": sr["source_id"],
                "status": sr["status"],
                "discovered": sr["discovered"],
                "accepted": sr["accepted"],
                "rejected": sr["rejected"],
                "quarantined": sr["quarantined"],
                "error": sr["error_message"],
            }
            for sr in source_runs
        ],
        "errors": [
            {"stage": e["stage"], "type": e["error_type"], "message": e["error_message"],
             "retryable": bool(e["retryable"]), "document_id": e["document_id"], "source_id": e["source_id"]}
            for e in errors
        ],
        "distributions": {
            "language": repo.distribution("language"),
            "destination": repo.distribution("destination"),
            "license": repo.distribution("normalized_license"),
            "source": repo.distribution("source_id"),
            "state": repo.distribution("state"),
            "distribution_scope": repo.distribution("distribution_scope"),
        },
        "coverage": coverage_report(profile),
        "duplicate_clusters": repo.duplicate_clusters(),
        "disk": {
            "corpus_bytes": storage.usage_bytes(),
            "corpus_bytes_human": human_bytes(storage.usage_bytes()),
            "free_bytes": free_disk_bytes(storage.root),
            "free_human": human_bytes(free_disk_bytes(storage.root)),
        },
        "ingested_documents": ingested,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def write_run_report(cfg: AppConfig, repo: CorpusRepository, run_id: str) -> tuple[Path, Path]:
    report = build_run_report(cfg, repo, run_id)

    runs_dir = cfg.outputs / "corpus" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    json_path = runs_dir / f"{run_id}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md_path = runs_dir / f"{run_id}.md"
    md_path.write_text(render_run_markdown(report), encoding="utf-8")

    return json_path, md_path


def render_run_markdown(report: dict) -> str:
    stats = report.get("stats", {})
    lines = [
        f"# Corpus harvest run `{report['run_id']}`",
        "",
        f"- **Profile**: {report.get('profile')} / rights `{report.get('rights_profile')}`",
        f"- **Mode**: {report.get('mode')}{' (dry run)' if report.get('dry_run') else ''}",
        f"- **Status**: {report.get('status')}",
        f"- **Started**: {report.get('started_at')}  →  **Finished**: {report.get('finished_at')}",
        f"- **Pipeline version**: {report.get('pipeline_version')}",
        "",
        "## Counters",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for label, key in [
        ("Candidates discovered", "discovered"),
        ("Already known", "already_known"),
        ("Rights accepted", "rights_accepted"),
        ("Rights rejected", "rights_rejected"),
        ("Rights quarantined", "rights_quarantined"),
        ("Selected for acquisition", "selected"),
        ("Downloaded", "downloaded"),
        ("Bytes downloaded", "bytes_downloaded"),
        ("Normalized", "normalized"),
        ("Quality rejected", "quality_rejected"),
        ("Duplicates", "duplicates"),
        ("Classified → books", "classified_books"),
        ("Classified → manifesto", "classified_manifesto"),
        ("Materialized", "materialized"),
        ("Ingested", "ingested"),
        ("Unchanged (idempotent skip)", "unchanged"),
        ("Errors", "errors"),
    ]:
        value = stats.get(key, 0)
        if key == "bytes_downloaded":
            value = f"{value} ({human_bytes(int(value or 0))})"
        lines.append(f"| {label} | {value} |")

    if stats.get("stopped_reason"):
        lines += ["", f"**Stopped early**: {stats['stopped_reason']}"]

    if stats.get("stage_seconds"):
        lines += ["", "## Time per stage", "", "| Stage | Seconds |", "|---|---:|"]
        lines += [f"| {stage} | {seconds} |" for stage, seconds in stats["stage_seconds"].items()]

    lines += ["", "## Per-source outcome", "", "| Source | Status | Discovered | Accepted | Rejected | Quarantined |",
              "|---|---|---:|---:|---:|---:|"]
    for source in report.get("sources", []):
        lines.append(
            f"| {source['source_id']} | {source['status']} | {source['discovered']} | "
            f"{source['accepted']} | {source['rejected']} | {source['quarantined']} |"
        )

    for title, key in [
        ("Languages", "language"), ("Destinations", "destination"),
        ("Licences", "license"), ("Sources", "source"), ("Pipeline states", "state"),
        ("Distribution scopes", "distribution_scope"),
    ]:
        distribution = report.get("distributions", {}).get(key, {})
        if not distribution:
            continue
        lines += ["", f"## {title}", "", "| Value | Count |", "|---|---:|"]
        lines += [f"| {k or '(unset)'} | {v} |" for k, v in distribution.items()]

    disk = report.get("disk", {})
    lines += [
        "", "## Disk", "",
        f"- Corpus size: {disk.get('corpus_bytes_human')}",
        f"- Free space: {disk.get('free_human')}",
    ]

    ingested = report.get("ingested_documents", [])
    lines += ["", f"## Documents in the index ({len(ingested)})", ""]
    if ingested:
        lines += ["| Title | Lang | Destination | Licence | Scope | Quality | Source |", "|---|---|---|---|---|---:|---|"]
        for doc in ingested[:200]:
            title = (doc["title"] or doc["document_id"])[:60].replace("|", "/")
            quality = f"{doc['quality_score']:.2f}" if doc.get("quality_score") is not None else "-"
            lines.append(
                f"| {title} | {doc['language'] or '?'} | {doc['destination']} | {doc['license']} | "
                f"{doc['distribution_scope']} | {quality} | {doc['source']} |"
            )
    else:
        lines.append("_No documents indexed in this run._")

    errors = report.get("errors", [])
    if errors:
        lines += ["", f"## Errors ({len(errors)})", "", "| Stage | Type | Retryable | Message |", "|---|---|---|---|"]
        for error in errors[:100]:
            message = (error["message"] or "")[:120].replace("|", "/").replace("\n", " ")
            lines.append(f"| {error['stage']} | {error['type']} | {error['retryable']} | {message} |")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Attributions
# --------------------------------------------------------------------------


def build_attributions(repo: CorpusRepository, items: list[ItemRecord] | None = None) -> list[dict]:
    items = items if items is not None else repo.all_items()
    records: list[dict] = []

    for item in items:
        if item.state not in (State.INGESTED, State.INDEXED, State.MATERIALIZED):
            continue
        decision = repo.latest_rights_decision(item.document_id) or {}
        attributions = repo.attributions(item.document_id)

        records.append(
            {
                "document_id": item.document_id,
                "title": item.title,
                "authors": item.authors,
                "contributors": [a["name"] for a in attributions if a["role"] == "contributor"],
                "translator": item.row.get("translator") or "",
                "source": next((a["name"] for a in attributions if a["role"] == "source"), item.source_id),
                "source_id": item.source_id,
                "canonical_url": item.canonical_url,
                "content_url": item.content_url,
                "license": item.normalized_license,
                "license_evidence_url": item.row.get("license_evidence_url") or "",
                "accessed_at": item.row.get("downloaded_at") or "",
                "verified_at": item.rights_verified_at or "",
                "attribution_required": item.attribution_required,
                "share_alike": item.share_alike,
                "distribution_scope": item.distribution_scope,
                "redistribution_permitted": bool(decision.get("redistribution")),
                "commercial_use_permitted": bool(decision.get("commercial_use")),
                "destination": item.destination.value if item.destination else "",
                "language": item.language,
                "normalized_sha256": item.normalized_sha256,
            }
        )

    records.sort(key=lambda r: (r["source_id"], r["title"] or "", r["document_id"]))
    return records


def write_attributions(cfg: AppConfig, repo: CorpusRepository, output_dir: Path | None = None) -> tuple[Path, Path]:
    records = build_attributions(repo)
    target = output_dir or (cfg.outputs / "corpus")
    target.mkdir(parents=True, exist_ok=True)

    jsonl_path = target / "ATTRIBUTIONS.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    md_path = target / "ATTRIBUTIONS.md"
    md_path.write_text(render_attributions_markdown(records), encoding="utf-8")

    return jsonl_path, md_path


def render_attributions_markdown(records: list[dict]) -> str:
    lines = [
        "# Corpus attributions",
        "",
        "Every automatically harvested document in the Field Horizon corpus, with the",
        "licence under which it was acquired and the obligations that licence imposes.",
        "",
        f"Generated {datetime.now(UTC).isoformat()} — {len(records)} documents.",
        "",
        "> Documents marked `LOCAL_US_ONLY` were accepted on the basis of a",
        "> United-States public-domain determination. They must not be included in a",
        "> worldwide release; `corpus export-safe --rights-profile release_worldwide`",
        "> refuses to package them.",
        "",
    ]

    by_license: dict[str, list[dict]] = {}
    for record in records:
        by_license.setdefault(record["license"] or "unknown", []).append(record)

    for license_name in sorted(by_license):
        group = by_license[license_name]
        lines += [f"## {license_name} ({len(group)} documents)", ""]
        for record in group:
            authors = ", ".join(record["authors"]) if record["authors"] else "(no author recorded)"
            obligations = []
            if record["attribution_required"]:
                obligations.append("attribution required")
            if record["share_alike"]:
                obligations.append("share-alike")
            obligation_text = f" — _{'; '.join(obligations)}_" if obligations else ""
            lines.append(
                f"- **{record['title'] or record['document_id']}** — {authors}. "
                f"Source: {record['source']}. "
                f"<{record['canonical_url']}> "
                f"(accessed {record['accessed_at'] or 'unknown'}; scope `{record['distribution_scope']}`)"
                f"{obligation_text}"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Source candidates
# --------------------------------------------------------------------------


def write_source_candidates(cfg: AppConfig, sources: list) -> Path:
    """
    Publish the disabled sources as reviewable candidates.

    A source is proposed here and stays DISABLED until its official
    endpoint is identified, its terms are documented, its licence mapping
    is tested, its rate limits are configured, and an integration test
    exists. This file is the visible record of that queue -- so a
    decision not to harvest something is as legible as a decision to
    harvest it, rather than being invisible in a config comment.
    """
    from .adapters.registry import available_adapters

    known = set(available_adapters())
    candidates = []
    for source in sources:
        if source.enabled:
            continue
        blockers = []
        if not source.base_url:
            blockers.append("no official endpoint identified")
        if not source.allowed_hosts:
            blockers.append("no host allowlist")
        if source.adapter not in known:
            blockers.append(f"no adapter named {source.adapter!r}")
        if not source.trusted_for_content_rights:
            blockers.append("provider is not trusted for content rights on its own word")
        if not source.notes:
            blockers.append("terms of use not documented")

        candidates.append(
            {
                "id": source.source_id,
                "name": source.display_name,
                "adapter": source.adapter,
                "status": "DISABLED",
                "base_url": source.base_url,
                "allowed_hosts": source.allowed_hosts,
                "trust": source.trust,
                "trusted_for_content_rights": source.trusted_for_content_rights,
                "blockers": blockers,
                "ready_to_enable": not blockers,
                "notes": source.notes,
            }
        )

    target = cfg.outputs / "corpus"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "source_candidates.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "_policy": (
                    "A candidate stays DISABLED until every blocker is cleared. "
                    "Enabling a source is a deliberate act, never a default."
                ),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# Export guard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    included: int
    excluded: int
    excluded_documents: list[dict]
    manifest_path: Path


def export_safe(
    cfg: AppConfig,
    repo: CorpusRepository,
    rights_profile_name: str,
    output_dir: Path,
    *,
    filter_incompatible: bool = False,
) -> ExportResult:
    """
    Package the subset of the corpus a rights profile permits.

    Without `filter_incompatible`, an incompatible document is a hard
    error naming every offender -- the operator must decide, not the
    tool. With it, incompatible documents are excluded and listed in the
    manifest, producing a genuinely publishable subset.

    A document qualifies only if its *latest* rights decision is an
    accept whose scope and permissions satisfy the target profile. The
    scope check is the one that matters: a Gutenberg text accepted under
    `local_research_us` is LOCAL_US_ONLY, and `release_worldwide`
    requires WORLDWIDE.
    """
    profile = get_profile(rights_profile_name)
    items = repo.all_items()

    included: list[ItemRecord] = []
    excluded: list[dict] = []

    for item in items:
        if item.state not in (State.INGESTED, State.INDEXED, State.MATERIALIZED):
            continue

        decision = repo.latest_rights_decision(item.document_id)
        reasons: list[str] = []

        if not decision or decision.get("decision") != "accept":
            reasons.append(f"latest rights decision is {decision.get('decision') if decision else 'missing'}")
        else:
            if decision.get("normalized_license") not in profile.allowed_licenses:
                reasons.append(
                    f"licence {decision.get('normalized_license')!r} is not permitted by {profile.name}"
                )
            scope = DistributionScope(item.distribution_scope or "UNKNOWN")
            if scope not in profile.allowed_scopes:
                reasons.append(f"distribution scope {scope.value} is not permitted by {profile.name}")
            if profile.require_commercial_use and not decision.get("commercial_use"):
                reasons.append("profile requires commercial use, which this licence does not grant")
            if profile.require_derivatives and not decision.get("derivatives"):
                reasons.append("profile requires derivatives, which this licence does not grant")

        if reasons:
            excluded.append(
                {
                    "document_id": item.document_id,
                    "title": item.title,
                    "license": item.normalized_license,
                    "distribution_scope": item.distribution_scope,
                    "reasons": reasons,
                }
            )
        else:
            included.append(item)

    if excluded and not filter_incompatible:
        preview = "\n".join(
            f"  - {e['document_id']} ({e['title'][:60]}): {'; '.join(e['reasons'])}" for e in excluded[:20]
        )
        more = f"\n  ... and {len(excluded) - 20} more" if len(excluded) > 20 else ""
        raise ExportBlocked(
            f"Refusing to export under {rights_profile_name!r}: "
            f"{len(excluded)} of {len(excluded) + len(included)} documents are not permitted.\n"
            f"{preview}{more}\n\n"
            f"Re-run with --filter to export only the {len(included)} permitted documents."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    texts_dir = output_dir / "texts"
    texts_dir.mkdir(parents=True, exist_ok=True)

    from .storage import CorpusStorage

    storage = CorpusStorage(cfg.root / "data" / "corpus")
    exported: list[dict] = []

    for item in included:
        if not item.normalized_sha256:
            continue
        blob = storage.normalized_path(item.normalized_sha256)
        if not blob.exists():
            logger.warning("Export: normalized artifact missing for %s; skipping", item.document_id)
            continue
        destination_dir = texts_dir / (item.destination.value if item.destination else "books")
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / Path(item.materialized_path or f"{item.document_id}.txt").name
        storage.link_or_copy(blob, target)
        exported.append({"document_id": item.document_id, "path": str(target.relative_to(output_dir))})

    attribution_records = build_attributions(repo, included)
    write_attributions(cfg, repo, output_dir=output_dir)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rights_profile": rights_profile_name,
        "profile_description": profile.description,
        "allowed_licenses": sorted(profile.allowed_licenses),
        "allowed_scopes": sorted(s.value for s in profile.allowed_scopes),
        "included_count": len(exported),
        "excluded_count": len(excluded),
        "filtered": filter_incompatible,
        "documents": exported,
        "excluded_documents": excluded,
        "attribution_count": len(attribution_records),
    }
    manifest_path = output_dir / "EXPORT_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return ExportResult(
        output_dir=output_dir,
        included=len(exported),
        excluded=len(excluded),
        excluded_documents=excluded,
        manifest_path=manifest_path,
    )


__all__ = [
    "ExportBlocked",
    "ExportResult",
    "RunLog",
    "build_attributions",
    "build_run_report",
    "export_safe",
    "render_attributions_markdown",
    "render_run_markdown",
    "write_attributions",
    "write_run_report",
    "write_source_candidates",
]
