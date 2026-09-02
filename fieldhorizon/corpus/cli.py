"""
`field-horizon corpus <verb>` -- the harvester's command surface.

Mounted into the existing `cli.build_parser()` as a single subparser
group, so there is one CLI, one config resolution path, and one help
tree. Everything here is a thin shell around the modules that do the
work: the CLI parses arguments, prints tables, and sets an exit code.

Exit codes: 0 success, 1 a real failure, 2 a refusal (budget not
configured, export blocked, lock held). A refusal is distinguished from
a failure so a systemd timer can treat "another run is in progress" as
routine rather than paging someone.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from ..config import load_config
from ..db import init_db
from .models import State
from .ocr import health_report as ocr_health
from .policy import CONTACT_ENV_VAR, PolicyError, describe_profiles, enabled_sources, load_policy, load_sources
from .reporting import (
    ExportBlocked,
    export_safe,
    write_attributions,
    write_run_report,
    write_source_candidates,
)
from .repository import CorpusRepository
from .scheduler import BudgetNotConfigured, LockHeld, RunLock, next_run_due
from .storage import CorpusStorage, free_disk_bytes, human_bytes, suggest_budget

logger = logging.getLogger(__name__)
console = Console()

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


def _load(args):
    """Config, policy, and sources, with argument overrides applied."""
    cfg = load_config(args.config)
    init_db(cfg.database)
    policy = load_policy(cfg, getattr(args, "policy_file", None))
    sources = load_sources(cfg, getattr(args, "sources_file", None))

    if getattr(args, "rights_profile", None):
        policy.rights_profile = args.rights_profile
    if getattr(args, "profile", None):
        policy.profile = args.profile
    if getattr(args, "max_items", None):
        policy.budget = _replace_budget(policy.budget, max_items_per_run=int(args.max_items))
    if getattr(args, "max_bytes", None):
        policy.budget = _replace_budget(policy.budget, max_download_bytes_per_run=int(args.max_bytes))
    if getattr(args, "no_llm", False):
        policy.use_llm_classifier = False

    return cfg, policy, sources


def _replace_budget(budget, **changes):
    from dataclasses import replace

    return replace(budget, **changes)


def _orchestrator(cfg, policy, sources, *, dry_run: bool = False):
    from .orchestrator import Orchestrator

    return Orchestrator(cfg, policy, sources, dry_run=dry_run)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_sources(args) -> None:
    cfg, policy, sources = _load(args)
    candidates_path = write_source_candidates(cfg, sources)

    if args.json:
        console.print_json(
            json.dumps(
                [
                    {
                        "id": s.source_id, "adapter": s.adapter, "name": s.display_name,
                        "enabled": s.enabled, "base_url": s.base_url, "trust": s.trust,
                        "trusted_for_content_rights": s.trusted_for_content_rights,
                        "allowed_hosts": s.allowed_hosts, "requests_per_minute": s.requests_per_minute,
                        "notes": s.notes,
                    }
                    for s in sources
                ]
            )
        )
        return

    table = Table(title="Corpus sources")
    table.add_column("ID")
    table.add_column("Adapter")
    table.add_column("Enabled", justify="center")
    table.add_column("Trust", justify="right")
    table.add_column("Rights trusted", justify="center")
    table.add_column("Hosts")

    for source in sources:
        table.add_row(
            source.source_id,
            source.adapter,
            "[green]yes[/green]" if source.enabled else "[dim]no[/dim]",
            f"{source.trust:.2f}",
            "yes" if source.trusted_for_content_rights else "[yellow]no[/yellow]",
            ", ".join(source.allowed_hosts[:3]) + (" …" if len(source.allowed_hosts) > 3 else ""),
        )
    console.print(table)

    disabled = [s for s in sources if not s.enabled]
    if disabled:
        console.print(
            f"\n[dim]{len(disabled)} source(s) disabled. A source stays disabled until its official "
            f"endpoint, terms, licence mapping, rate limits, and integration test all exist.\n"
            f"Candidates and their blockers: {candidates_path}[/dim]"
        )


def cmd_health(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    problems: list[str] = []
    table = Table(title="Corpus harvester health")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    contact = __import__("os").environ.get(CONTACT_ENV_VAR, "").strip()
    table.add_row(
        "Contact address",
        "[green]set[/green]" if contact else "[yellow]unset[/yellow]",
        contact or f"Set ${CONTACT_ENV_VAR} — institutional sources require a reachable contact",
    )
    if not contact:
        problems.append(f"${CONTACT_ENV_VAR} is not set")

    try:
        policy = load_policy(cfg, getattr(args, "policy_file", None))
        table.add_row("Policy file", "[green]ok[/green]", f"profile={policy.profile}, rights={policy.rights_profile}")
    except PolicyError as exc:
        policy = None
        table.add_row("Policy file", "[red]missing[/red]", str(exc)[:100])
        problems.append("policy file missing or invalid")

    try:
        sources = load_sources(cfg, getattr(args, "sources_file", None))
        active = enabled_sources(sources, policy.profile if policy else "broad")
        table.add_row("Sources", "[green]ok[/green]", f"{len(active)} enabled of {len(sources)} configured")
    except PolicyError as exc:
        table.add_row("Sources", "[red]invalid[/red]", str(exc)[:100])
        problems.append("source configuration invalid")

    # OCR is optional and stays optional. This row exists so an operator
    # can tell "no backend installed" from "backend present, policy says
    # no" without reading two config files -- and so a corpus full of
    # OCR_PENDING documents has an obvious explanation.
    ocr_status = ocr_health(
        policy.ocr_enabled if policy else False,
        getattr(policy, "max_ocr_documents_per_run", 5) if policy else 5,
    )
    backend = ocr_status["backend"]
    if not backend["available"]:
        ocr_label, ocr_colour = "not installed", "yellow"
    elif ocr_status["would_run"]:
        ocr_label, ocr_colour = "ready", "green"
    else:
        ocr_label, ocr_colour = "available, disabled", "yellow"
    table.add_row(
        "OCR",
        f"[{ocr_colour}]{ocr_label}[/{ocr_colour}]",
        (
            f"{backend['name']} ({backend['kind']}), ceiling "
            f"{ocr_status['max_documents_per_run']}/run"
            if backend["available"]
            # Escaped: rich reads square brackets as markup, and the
            # unescaped reason printed `pip install 'fieldhorizon'`
            # instead of `pip install 'fieldhorizon[ocr]'` -- an
            # instruction that runs cleanly and installs the wrong thing.
            else rich_escape(ocr_status["reason"])[:160]
        ),
    )

    free = free_disk_bytes(cfg.root)
    if policy:
        budget = policy.budget
        configured = budget.max_total_corpus_bytes > 0 and budget.min_free_disk_bytes > 0
        table.add_row(
            "Storage budget",
            "[green]configured[/green]" if configured else "[red]not configured[/red]",
            f"ceiling={human_bytes(budget.max_total_corpus_bytes)}, floor={human_bytes(budget.min_free_disk_bytes)}"
            if configured
            else "autonomous modes will refuse to start",
        )
        if not configured:
            problems.append("storage budget not configured")

    used = storage.usage_bytes()
    table.add_row("Disk", "[green]ok[/green]", f"corpus={human_bytes(used)}, free={human_bytes(free)}")

    repo = CorpusRepository(cfg.database)
    counts = repo.count_items_by_state()
    table.add_row("Documents", "[green]ok[/green]", f"{sum(counts.values())} tracked across {len(counts)} states")

    console.print(table)

    if problems:
        console.print("\n[yellow]Issues:[/yellow]")
        for problem in problems:
            console.print(f"  • {problem}")
        if "storage budget not configured" in problems:
            suggestion = suggest_budget(cfg.root)
            console.print("\n[dim]Suggested budget for config/corpus_policy.yaml (review before adopting):[/dim]")
            console.print(f"[dim]budget:\n"
                          f"  max_total_corpus_bytes: {suggestion['max_total_corpus_bytes']}\n"
                          f"  min_free_disk_bytes: {suggestion['min_free_disk_bytes']}\n"
                          f"  max_download_bytes_per_run: {suggestion['max_download_bytes_per_run']}[/dim]")
        sys.exit(EXIT_REFUSED)


def cmd_profiles(args) -> None:
    table = Table(title="Acquisition profiles")
    table.add_column("Name")
    table.add_column("Max items/run", justify="right")
    table.add_column("Min source trust", justify="right")
    table.add_column("Description")
    for profile in describe_profiles():
        name = profile["name"]
        if profile["requires_explicit_budget"]:
            name += " [yellow](explicit budget)[/yellow]"
        table.add_row(name, str(profile["max_items_per_run"]), f"{profile['min_source_trust']:.2f}",
                      profile["description"])
    console.print(table)


def cmd_discover(args) -> None:
    cfg, policy, sources = _load(args)
    orch = _orchestrator(cfg, policy, sources, dry_run=True)
    count = orch.discover(policy.profile)
    console.print(f"[green]Discovered[/green] {count} new candidate(s) across {len(enabled_sources(sources, policy.profile))} source(s).")
    console.print(f"Rights: {orch.stats.rights_accepted} accepted, "
                  f"{orch.stats.rights_rejected} rejected, {orch.stats.rights_quarantined} quarantined.")
    console.print(f"Run id: [cyan]{orch.run_id}[/cyan]")


def cmd_plan(args) -> None:
    cfg, policy, sources = _load(args)
    orch = _orchestrator(cfg, policy, sources, dry_run=True)

    if not args.no_discover:
        orch.discover(policy.profile)

    plan = orch.plan(args.limit)

    storage = CorpusStorage(cfg.root / "data" / "corpus")
    plan_path = storage.reports / f"{orch.run_id}_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps({"run_id": orch.run_id, "profile": policy.profile,
                    "rights_profile": policy.rights_profile, "entries": plan},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json:
        console.print_json(json.dumps(plan))
    else:
        table = Table(title=f"Acquisition plan ({len(plan)} documents)")
        table.add_column("#", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Title")
        table.add_column("Lang")
        table.add_column("Format")
        table.add_column("Source")
        for index, entry in enumerate(plan[:60], start=1):
            table.add_row(
                str(index), f"{entry['score']:.3f}", (entry["title"] or entry["external_id"])[:56],
                entry["language"] or "?", entry["format"] or "?", entry["source_id"],
            )
        console.print(table)
        if len(plan) > 60:
            console.print(f"[dim]… and {len(plan) - 60} more[/dim]")

    console.print(f"\nPlan written to [cyan]{plan_path}[/cyan]")
    if args.dry_run:
        console.print("[dim]Dry run: nothing was downloaded.[/dim]")


def cmd_sync(args) -> None:
    cfg, policy, sources = _load(args)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    try:
        with RunLock(storage.locks):
            orch = _orchestrator(cfg, policy, sources, dry_run=args.dry_run)
            stats = orch.run_sync(policy.profile, args.limit)
            json_path, md_path = write_run_report(cfg, orch.repo, orch.run_id)
            write_attributions(cfg, orch.repo)
            write_source_candidates(cfg, sources)
    except LockHeld as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        sys.exit(EXIT_REFUSED)
    except BudgetNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(EXIT_REFUSED)

    _print_stats(stats, orch.run_id)
    console.print(f"\nReport: [cyan]{md_path}[/cyan]\n        [cyan]{json_path}[/cyan]")


def cmd_resume(args) -> None:
    cfg, policy, sources = _load(args)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    try:
        with RunLock(storage.locks):
            orch = _orchestrator(cfg, policy, sources)
            stats = orch.resume()
            json_path, md_path = write_run_report(cfg, orch.repo, orch.run_id)
    except LockHeld as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        sys.exit(EXIT_REFUSED)
    except BudgetNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(EXIT_REFUSED)

    _print_stats(stats, orch.run_id)
    console.print(f"\nReport: [cyan]{md_path}[/cyan]")


def cmd_status(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    item_states = repo.count_items_by_state()
    candidate_states = repo.count_candidates_by_state()
    last = repo.last_run()

    payload: dict[str, Any] = {
        "candidates_by_state": candidate_states,
        "items_by_state": item_states,
        "total_candidates": sum(candidate_states.values()),
        "total_items": sum(item_states.values()),
        "indexed": item_states.get(State.INDEXED.value, 0) + item_states.get(State.INGESTED.value, 0),
        "quarantined": candidate_states.get(State.RIGHTS_QUARANTINED.value, 0),
        "distributions": {
            "language": repo.distribution("language"),
            "destination": repo.distribution("destination"),
            "license": repo.distribution("normalized_license"),
            "source": repo.distribution("source_id"),
            "distribution_scope": repo.distribution("distribution_scope"),
        },
        "disk": {
            "corpus_bytes": storage.usage_bytes(),
            "free_bytes": free_disk_bytes(storage.root),
        },
        "last_run": last,
    }

    if args.json:
        console.print_json(json.dumps(payload, default=str))
        return

    table = Table(title="Corpus harvester status")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Candidates discovered", str(payload["total_candidates"]))
    table.add_row("Documents acquired", str(payload["total_items"]))
    table.add_row("Documents indexed", str(payload["indexed"]))
    table.add_row("Rights-quarantined candidates", str(payload["quarantined"]))
    table.add_row("Corpus size", human_bytes(payload["disk"]["corpus_bytes"]))
    table.add_row("Free disk", human_bytes(payload["disk"]["free_bytes"]))
    console.print(table)

    if item_states:
        state_table = Table(title="Documents by pipeline state")
        state_table.add_column("State")
        state_table.add_column("Count", justify="right")
        for state, count in sorted(item_states.items(), key=lambda kv: -kv[1]):
            state_table.add_row(state, str(count))
        console.print(state_table)

    for title, key in (("Languages", "language"), ("Destinations", "destination"), ("Licences", "license")):
        distribution = payload["distributions"][key]
        if not distribution:
            continue
        dist_table = Table(title=title)
        dist_table.add_column("Value")
        dist_table.add_column("Count", justify="right")
        for value, count in list(distribution.items())[:12]:
            dist_table.add_row(value or "(unset)", str(count))
        console.print(dist_table)

    if last:
        console.print(
            f"\nLast run [cyan]{last['run_id']}[/cyan] ({last['mode']}, {last['status']}) "
            f"started {last['started_at']}"
        )


def cmd_report(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    run_id = args.run_id
    if not run_id:
        last = repo.last_run()
        if not last:
            console.print("[yellow]No harvester runs recorded yet.[/yellow]")
            return
        run_id = last["run_id"]

    json_path, md_path = write_run_report(cfg, repo, run_id)
    console.print(f"[green]Report written:[/green]\n  {md_path}\n  {json_path}")
    if args.show:
        console.print(md_path.read_text(encoding="utf-8"))


def cmd_audit_rights(args) -> None:
    cfg, policy, sources = _load(args)
    orch = _orchestrator(cfg, policy, sources)
    results = orch.audit_rights(args.stale_after_days)

    if args.json:
        console.print_json(json.dumps(results))
        return

    table = Table(title=f"Rights audit (stale after {args.stale_after_days} days)")
    table.add_column("Outcome")
    table.add_column("Count", justify="right")
    table.add_row("Checked", str(results["checked"]))
    table.add_row("Still valid", str(results["still_valid"]))
    table.add_row("Marked stale (de-indexed)", str(results["stale"]))
    table.add_row("Withdrawn", str(results["withdrawn"]))
    console.print(table)

    if results["details"]:
        console.print("\n[dim]Files, blobs, sidecars, and every prior decision row are preserved. "
                      "De-indexing removes a document from retrieval, not from the record.[/dim]")


def cmd_quarantine(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    rows = repo.candidates_in_state(State.RIGHTS_QUARANTINED, limit=args.limit)
    if args.json:
        console.print_json(json.dumps(rows, default=str))
        return

    table = Table(title=f"Rights-quarantined candidates ({len(rows)})")
    table.add_column("Source")
    table.add_column("Title")
    table.add_column("Reason codes")
    for row in rows:
        repo.latest_rights_decision(row["candidate_id"])
        codes = ""
        with __import__("sqlite3").connect(cfg.database) as conn:
            conn.row_factory = __import__("sqlite3").Row
            found = conn.execute(
                "SELECT reason_codes FROM corpus_rights_decisions WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
                (row["candidate_id"],),
            ).fetchone()
            if found:
                codes = ", ".join(json.loads(found["reason_codes"] or "[]"))
        table.add_row(row["source_id"], (row["title"] or row["external_id"])[:56], codes[:70])
    console.print(table)
    console.print("\n[dim]No quarantined document is ever indexed. Quarantine is reviewable, not silent.[/dim]")


def cmd_reclassify(args) -> None:
    cfg, policy, sources = _load(args)
    repo = CorpusRepository(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    from . import classification as classification_mod

    targets = (
        [repo.get_item(args.document_id)]
        if args.document_id
        else [
            item
            for item in repo.all_items()
            if item.state in (State.INGESTED, State.INDEXED, State.MATERIALIZED,
                              State.CLASSIFIED_BOOKS, State.CLASSIFIED_MANIFESTO)
            and (not args.low_confidence_only or item.row.get("classification_low_confidence"))
        ]
    )
    targets = [t for t in targets if t is not None]

    changed = 0
    for item in targets:
        if not item.normalized_sha256:
            continue
        try:
            text = storage.read_normalized(item.normalized_sha256)
        except OSError:
            continue

        result = classification_mod.classify(
            title=item.title,
            document_type=item.row.get("document_type") or "",
            subjects=json.loads(item.row.get("subjects") or "[]"),
            text=text,
            cfg=cfg,
            use_llm=policy.use_llm_classifier,
            model=policy.classifier_model or None,
            threshold=policy.manifesto_threshold,
        )
        repo.record_classification(item.document_id, result)

        if item.destination != result.destination:
            changed += 1
            repo.update_item(
                item.document_id,
                destination=result.destination.value,
                manifesto_score=result.manifesto_score,
                classification_confidence=result.confidence,
                classification_low_confidence=int(result.low_confidence),
                classifier_version=result.classifier_version,
                classifier_kind=result.classifier_kind,
            )
            console.print(
                f"  {item.title[:50]}: "
                f"[yellow]{item.destination.value if item.destination else '?'}[/yellow] → "
                f"[green]{result.destination.value}[/green]"
            )

    console.print(f"\n[green]Reclassified[/green] {len(targets)} document(s); {changed} changed destination.")
    if changed:
        console.print("[dim]Run `field-horizon corpus rematerialize` to move the changed documents.[/dim]")


def cmd_renormalize(args) -> None:
    cfg, policy, sources = _load(args)
    orch = _orchestrator(cfg, policy, sources)
    results = orch.renormalize(reclassify=not args.no_reclassify)

    table = Table(title="Re-normalization from stored blobs (no network)")
    table.add_column("Outcome")
    table.add_column("Count", justify="right")
    for key in ("examined", "renormalized", "unchanged", "failed"):
        table.add_row(key, str(results[key]))
    console.print(table)

    if results["renormalized"]:
        console.print(
            f"\n[dim]{results['renormalized']} document(s) produced different text. "
            f"Run `corpus rematerialize` to write the new text into the corpus tree "
            f"and re-ingest it.[/dim]"
        )
    if results["failed"]:
        for detail in results["details"][:10]:
            if "reason" in detail:
                console.print(f"  [yellow]{detail['document_id']}[/yellow]: {detail['reason']}")


def cmd_rematerialize(args) -> None:
    cfg, policy, sources = _load(args)
    repo = CorpusRepository(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    from .ingestion import ingest_document, withdraw_document
    from .materialization import dematerialize, materialize

    moved = 0
    for item in repo.all_items():
        if item.state not in (State.MATERIALIZED, State.INGESTED, State.INDEXED):
            continue
        if item.destination is None:
            continue
        expected = str(item.materialized_path or "")
        from .materialization import destination_dir, materialized_filename

        target = destination_dir(cfg, item.destination) / materialized_filename(item)

        # A document needs rewriting if its DESTINATION moved *or* if its
        # TEXT changed -- `corpus renormalize` rebuilds text from the raw
        # blobs after a normalizer improvement, and checking only the path
        # left those documents materialized and indexed with their old,
        # stale text while reporting nothing to do.
        stale_text = False
        if target.exists() and item.normalized_sha256:
            from ..corpus.security import sha256_text

            try:
                stale_text = sha256_text(target.read_text(encoding="utf-8")) != item.normalized_sha256
            except OSError:
                stale_text = True

        if expected == str(target) and target.exists() and not stale_text:
            continue

        withdraw_document(cfg, item.document_id)
        dematerialize(cfg, item)
        result = materialize(cfg, repo, storage, item)
        repo.update_item(item.document_id, materialized_path=str(result.path))
        refreshed = repo.get_item(item.document_id)
        if refreshed:
            ingest_document(cfg, repo, refreshed)
        moved += 1
        reason = "text changed" if stale_text else "destination changed"
        console.print(f"  rewrote {item.document_id} ({reason}) → {result.path}")

    console.print(f"[green]Rematerialized[/green] {moved} document(s).")


def cmd_ingest(args) -> None:
    cfg, policy, sources = _load(args)
    repo = CorpusRepository(cfg.database)

    from .ingestion import ingest_document

    counts = {"ingested": 0, "reingested": 0, "unchanged": 0, "skipped": 0}
    for item in repo.all_items():
        if item.state not in (State.MATERIALIZED, State.INGESTED, State.INDEXED):
            continue
        outcome = ingest_document(cfg, repo, item)
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        if outcome.status == "skipped" and args.verbose:
            console.print(f"  [dim]skipped {item.document_id}: {outcome.reason}[/dim]")

    table = Table(title="Corpus ingestion")
    table.add_column("Outcome")
    table.add_column("Count", justify="right")
    for status, count in counts.items():
        table.add_row(status, str(count))
    console.print(table)


def cmd_export_safe(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    try:
        result = export_safe(
            cfg, repo, args.rights_profile, Path(args.output), filter_incompatible=args.filter
        )
    except ExportBlocked as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(EXIT_REFUSED)

    console.print(
        f"[green]Exported[/green] {result.included} document(s) to {result.output_dir}"
        + (f", excluding {result.excluded}" if result.excluded else "")
    )
    console.print(f"Manifest: [cyan]{result.manifest_path}[/cyan]")
    if result.excluded:
        console.print("\n[yellow]Excluded documents:[/yellow]")
        for excluded in result.excluded_documents[:15]:
            console.print(f"  • {excluded['title'][:50]}: {'; '.join(excluded['reasons'])}")


def cmd_attributions(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)
    jsonl_path, md_path = write_attributions(cfg, repo)
    console.print(f"[green]Attributions written:[/green]\n  {md_path}\n  {jsonl_path}")


def cmd_show(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    item = repo.get_item(args.document_id)
    if item is None:
        console.print(f"[red]No such document:[/red] {args.document_id}")
        sys.exit(EXIT_FAILURE)

    payload: dict[str, Any] = {
        "item": item.row,
        "rights_decisions": repo.rights_decisions(item.document_id),
        "classification": repo.latest_classification(item.document_id),
        "attributions": repo.attributions(item.document_id),
        "artifacts": repo.artifacts(item.document_id),
        "state_events": repo.state_events(item.document_id),
    }

    if args.json:
        console.print_json(json.dumps(payload, default=str))
        return

    console.print(f"[bold]{item.title}[/bold]  [dim]({item.document_id})[/dim]")
    console.print(f"  state: {item.state.value}    destination: {item.destination.value if item.destination else '-'}")
    console.print(f"  licence: {item.normalized_license}    scope: {item.distribution_scope}")
    console.print(f"  source: {item.source_id}    url: {item.canonical_url}")
    console.print(f"  quality: {item.quality_score}    language: {item.language}")

    events = Table(title="State history")
    events.add_column("When")
    events.add_column("From → To")
    events.add_column("Reason")
    for event in payload["state_events"]:
        events.add_row(event["occurred_at"], f"{event['old_state'] or '-'} → {event['new_state']}",
                       (event["reason"] or "")[:70])
    console.print(events)


def cmd_daemon_once(args) -> None:
    """
    One bounded cycle, then exit. Never a loop.

    Designed for a systemd timer: honours the configured sync interval,
    exits 0 when a run is not yet due, and treats a held lock as routine
    rather than as a failure.
    """
    cfg, policy, sources = _load(args)
    repo = CorpusRepository(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")

    last = repo.last_run()
    if last and not args.force and not next_run_due(last.get("finished_at"), policy.sync_interval_hours):
        console.print(
            f"[dim]Last run finished {last.get('finished_at')}; "
            f"next due after {policy.sync_interval_hours}h. Nothing to do.[/dim]"
        )
        return

    try:
        with RunLock(storage.locks):
            orch = _orchestrator(cfg, policy, sources)
            stats = orch.run_sync(policy.profile, args.limit)
            write_run_report(cfg, orch.repo, orch.run_id)
            write_attributions(cfg, orch.repo)
    except LockHeld as exc:
        console.print(f"[dim]{exc}[/dim]")
        return
    except BudgetNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(EXIT_REFUSED)

    _print_stats(stats, orch.run_id)


def _print_stats(stats, run_id: str) -> None:
    table = Table(title=f"Harvest run {run_id}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    data = stats.to_dict()
    for label, key in [
        ("Discovered", "discovered"), ("Already known", "already_known"),
        ("Rights accepted", "rights_accepted"), ("Rights rejected", "rights_rejected"),
        ("Rights quarantined", "rights_quarantined"), ("Selected", "selected"),
        ("Downloaded", "downloaded"), ("Normalized", "normalized"),
        ("Quality rejected", "quality_rejected"), ("Duplicates", "duplicates"),
        ("→ books", "classified_books"), ("→ manifesto", "classified_manifesto"),
        ("Materialized", "materialized"), ("Ingested", "ingested"),
        ("Unchanged", "unchanged"), ("Errors", "errors"),
    ]:
        table.add_row(label, str(data.get(key, 0)))
    table.add_row("Bytes downloaded", human_bytes(int(data.get("bytes_downloaded", 0))))
    console.print(table)

    if data.get("stopped_reason"):
        console.print(f"[yellow]Stopped early:[/yellow] {data['stopped_reason']}")


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def add_corpus_parser(subparsers) -> None:
    """
    Mount `corpus` into the main CLI's subparsers.

    Called once from `cli.build_parser()`. Kept here so the harvester's
    ~15 verbs do not add 200 lines to an already 1400-line cli.py.
    """
    parser = subparsers.add_parser(
        "corpus",
        help="Autonomous Open Corpus Harvester: discover, verify, acquire, classify, and index open texts.",
    )
    verbs = parser.add_subparsers(dest="corpus_command", required=True)

    def common(sub):
        sub.add_argument("--policy-file", default=None, help="Override config/corpus_policy.yaml")
        sub.add_argument("--sources-file", default=None, help="Override config/corpus_sources.yaml")
        sub.add_argument("--rights-profile", default=None,
                         choices=["local_research_us", "release_worldwide", "strict_public_domain"])
        sub.add_argument("--profile", default=None, choices=["seed", "broad", "archive"])
        return sub

    s = verbs.add_parser("sources", help="List configured sources and their trust settings.")
    common(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sources)

    s = verbs.add_parser("health", help="Check contact, policy, budget, and disk before harvesting.")
    s.add_argument("--policy-file", default=None)
    s.add_argument("--sources-file", default=None)
    s.set_defaults(func=cmd_health)

    s = verbs.add_parser("profiles", help="Describe the seed/broad/archive acquisition profiles.")
    s.set_defaults(func=cmd_profiles)

    s = verbs.add_parser("discover", help="Walk catalogues and record candidates. Downloads no content.")
    common(s)
    s.set_defaults(func=cmd_discover)

    s = verbs.add_parser("plan", help="Rank admissible candidates and write the acquisition plan.")
    common(s)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--dry-run", action="store_true", help="Explicit no-op marker; plan never downloads.")
    s.add_argument("--no-discover", action="store_true", help="Plan from already-discovered candidates only.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    s = verbs.add_parser("sync", help="Full bounded cycle: discover, plan, acquire, classify, ingest.")
    common(s)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--max-items", type=int, default=None)
    s.add_argument("--max-bytes", type=int, default=None)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-llm", action="store_true", help="Deterministic classification only.")
    s.set_defaults(func=cmd_sync)

    s = verbs.add_parser("resume", help="Continue whatever an interrupted run left behind.")
    common(s)
    s.add_argument("--max-items", type=int, default=None)
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(func=cmd_resume)

    s = verbs.add_parser("status", help="Corpus state, distributions, and disk usage.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = verbs.add_parser("report", help="Write the Markdown + JSON report for a run.")
    s.add_argument("--run-id", default=None, help="Defaults to the most recent run.")
    s.add_argument("--show", action="store_true")
    s.set_defaults(func=cmd_report)

    s = verbs.add_parser("audit-rights", help="Re-verify licence evidence; de-index anything stale.")
    common(s)
    s.add_argument("--stale-after-days", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_audit_rights)

    s = verbs.add_parser("quarantine", help="List rights-quarantined candidates and why.")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_quarantine)

    s = verbs.add_parser("reclassify", help="Re-run books/manifesto classification.")
    common(s)
    s.add_argument("--document-id", default=None)
    s.add_argument("--low-confidence-only", action="store_true")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(func=cmd_reclassify)

    s = verbs.add_parser(
        "renormalize",
        help="Rebuild normalized text from the stored raw blobs after a normalizer change. No network.",
    )
    common(s)
    s.add_argument("--no-reclassify", action="store_true",
                   help="Refresh the text but keep the existing books/manifesto destination.")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(func=cmd_renormalize)

    s = verbs.add_parser("rematerialize", help="Move documents whose destination changed.")
    common(s)
    s.set_defaults(func=cmd_rematerialize)

    s = verbs.add_parser("ingest", help="Ingest materialized documents into the Field Horizon index.")
    common(s)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = verbs.add_parser("export-safe", help="Package the subset a rights profile permits.")
    s.add_argument("--rights-profile", required=True,
                   choices=["local_research_us", "release_worldwide", "strict_public_domain"])
    s.add_argument("--output", required=True)
    s.add_argument("--filter", action="store_true",
                   help="Exclude incompatible documents instead of refusing the whole export.")
    s.set_defaults(func=cmd_export_safe)

    s = verbs.add_parser("attributions", help="Regenerate ATTRIBUTIONS.jsonl and ATTRIBUTIONS.md.")
    s.set_defaults(func=cmd_attributions)

    s = verbs.add_parser("show", help="Full dossier for one document: rights, classification, history.")
    s.add_argument("document_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = verbs.add_parser("daemon-once", help="One bounded cycle then exit. For the systemd timer.")
    common(s)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--max-items", type=int, default=None)
    s.add_argument("--force", action="store_true", help="Run even if the interval has not elapsed.")
    s.set_defaults(func=cmd_daemon_once)


__all__ = ["add_corpus_parser"]
