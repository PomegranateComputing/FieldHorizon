from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from .agents import AGENTS, AgentOutput
from .concepts import tag_chunks
from .config import load_config
from .dashboard import render_weather_tui, write_weather_html
from .db import connect, init_db
from .dream import run_dream
from .embeddings import backfill_axiom_embeddings, backfill_canon_embeddings, backfill_chunk_embeddings
from .engine import FieldHorizonEngine
from .events import EventRepository
from .export import export_codex
from .fingerprint import compare_fingerprints
from .godot_export import export_godot
from .indexer import build_cycle_index
from .ingest import backfill_chunk_offsets, ingest_books, ingest_from_manifest, ingest_json_corpus, ingest_manifestos
from .lineage import LineageNotFoundError, build_lineage_tree
from .manifest import ManifestError
from .multicycle import run_multi_cycle
from .ontology import (
    export_axiom_candidates,
    export_generated_axioms,
    export_pressure_report,
)
from .proposals import ProposalError, apply_proposal, list_proposals, reject_proposal, show_proposal
from .provenance import (
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    add_provenance_graph_section,
    backfill_provenance_edges,
    build_provenance_report_for_target,
)
from .registries import list_registry, show_registry_entry
from .replay import (
    ReplayNotFoundError,
    replay_cycle,
    replay_cycle_against_historical_canon,
    replay_cycle_level2,
    replay_cycle_level3,
    replay_cycle_level5,
    replay_schools_level4,
)
from .rustcore import export_rust_pressure_report, export_rust_pressure_report_all
from .schools import run_schools
from .server import run_server
from .temporal import backfill_canon_temporal_states
from .weather import backfill_canon_weather

logging.basicConfig(
    level=logging.WARNING,
    format="%(name)s: %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)

console = Console()


def cmd_init(args) -> None:
    cfg = load_config(args.config)
    for p in [cfg.books, cfg.json_corpus, cfg.outputs, cfg.logs]:
        p.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)
    console.print(f"[green]Initialized Field Horizon DB:[/green] {cfg.database}")


def cmd_ingest(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    book_count = ingest_books(cfg) if args.all or args.books else 0
    json_count = ingest_json_corpus(cfg) if args.all or args.json else 0
    console.print(f"[green]Ingested book chunks:[/green] {book_count}")
    console.print(f"[green]Ingested JSON entries:[/green] {json_count}")
    manifesto_count = ingest_manifestos(cfg) if args.all or args.manifestos else 0
    console.print(f"[green]Ingested manifesto chunks:[/green] {manifesto_count}")


def cmd_status(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        sources = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        json_entries = conn.execute("SELECT COUNT(*) AS n FROM json_entries").fetchone()["n"]
        cycles = conn.execute("SELECT COUNT(*) AS n FROM cycles").fetchone()["n"]
    console.print(f"Sources: {sources}")
    console.print(f"Book chunks: {chunks}")
    console.print(f"JSON entries: {json_entries}")
    console.print(f"Cycles: {cycles}")


def cmd_stats(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    stats = FieldHorizonEngine(cfg).stats()

    table = Table(title="Field Horizon Stats")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for label, value in [
        ("Cycles (total)", stats.cycles_total),
        ("Canon", stats.canon_count),
        ("Heresy", stats.heresy_count),
        ("Useful fragment", stats.useful_fragment_count),
        ("Noise", stats.noise_count),
        ("Sources", stats.sources_count),
        ("Chunks", stats.chunks_count),
        ("Councils", stats.councils_count),
        ("School clustering runs", stats.schools_count),
        ("Dream runs", stats.dream_runs_count),
    ]:
        table.add_row(label, str(value))
    console.print(table)


def cmd_cycle(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    cycle_id = FieldHorizonEngine(cfg).cycle(
        args.query,
        model=args.model,
        dry_run=args.dry_run,
        auto_rewrite=args.auto_rewrite,
        critic_model=args.critic_model,
        rewrite_model=args.rewrite_model,
        json_domain=args.json_domain,
    )
    console.print(f"[green]Cycle saved:[/green] {cycle_id}")

def _agent_panel(name: str, content: str | None) -> Panel:
    if content is None:
        return Panel("[dim]waiting...[/dim]", title=name, border_style="dim")
    if not content:
        return Panel("[red]failed to produce a memorandum[/red]", title=name, border_style="red")
    return Panel(content, title=name, border_style="green")


def _synthesis_panel(content: str | None) -> Panel:
    if content is None:
        return Panel("[dim]waiting for agents...[/dim]", title="SYNTHESIS", border_style="dim")
    return Panel(content, title="SYNTHESIS", border_style="cyan")


def _build_multicycle_layout() -> Layout:
    layout = Layout()
    layout.split_column(Layout(name="agents"), Layout(name="synthesis", size=12))
    layout["agents"].split_row(*(Layout(name=agent["name"]) for agent in AGENTS))

    for agent in AGENTS:
        layout[agent["name"]].update(_agent_panel(agent["name"], None))
    layout["synthesis"].update(_synthesis_panel(None))

    return layout


def cmd_multi_cycle(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    on_agent_done = None
    on_synthesis_done = None
    live_cm: Live | None = None

    if args.live and not args.dry_run:
        layout = _build_multicycle_layout()
        live_cm = Live(layout, console=console, refresh_per_second=4)

        def on_agent_done(output: AgentOutput) -> None:  # noqa: F811
            layout[output.name].update(_agent_panel(output.name, output.content))

        def on_synthesis_done(fragment: str) -> None:  # noqa: F811
            layout["synthesis"].update(_synthesis_panel(fragment))

    def run() -> int:
        return run_multi_cycle(
            cfg=cfg,
            query=args.query,
            model=args.model,
            agent_model=args.agent_model,
            synthesizer_model=args.synthesizer_model,
            critic_model=args.critic_model,
            rewrite_model=args.rewrite_model,
            dry_run=args.dry_run,
            auto_rewrite=args.auto_rewrite,
            json_domain=args.json_domain,
            interpreter_model=args.interpreter_model,
            on_agent_done=on_agent_done,
            on_synthesis_done=on_synthesis_done,
        )

    if live_cm is not None:
        with live_cm:
            cycle_id = run()
    else:
        cycle_id = run()

    console.print(f"[green]Multi-cycle saved:[/green] {cycle_id}")

def cmd_export(args) -> None:
    cfg = load_config(args.config)
    path = export_codex(cfg)
    console.print(f"[green]Codex exported:[/green] {path}")


def _events_table(title: str, events: list) -> Table:
    table = Table(title=title)
    table.add_column("Event id")
    table.add_column("Type")
    table.add_column("Actor")
    table.add_column("Aggregate")
    table.add_column("Recorded at")
    for event in events:
        aggregate = f"{event.aggregate_type}:{event.aggregate_id}" if event.aggregate_id else event.aggregate_type
        table.add_row(event.event_id, event.event_type, event.actor, aggregate, event.recorded_at)
    return table


def cmd_events(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    if args.events_action == "list":
        events = repo.recent(limit=args.limit)
        if not events:
            console.print("[dim]No events recorded yet.[/dim]")
            return
        console.print(_events_table("Recent Events", events))
        return

    if args.events_action == "show":
        event = repo.get(args.event_id)
        if event is None:
            console.print(f"[red]No event found with id {args.event_id!r}[/red]")
            raise SystemExit(1)
        console.print(f"[bold]{event.event_type}[/bold]  ({event.event_id})")
        console.print(f"actor: {event.actor}")
        console.print(f"aggregate: {event.aggregate_type}:{event.aggregate_id}")
        console.print(f"correlation_id: {event.correlation_id}")
        console.print(f"causation_id: {event.causation_id}")
        console.print(f"run_id: {event.run_id}")
        console.print(f"canon_event_id: {event.canon_event_id}")
        console.print(f"occurred_at: {event.occurred_at}  |  recorded_at: {event.recorded_at}")
        console.print("payload:")
        console.print(yaml_dump_for_display(event.payload))
        return

    if args.events_action == "trace":
        events = repo.by_correlation(args.correlation_id)
        if not events:
            console.print(f"[yellow]No events found for correlation id {args.correlation_id!r}[/yellow]")
            return
        console.print(_events_table(f"Trace: correlation {args.correlation_id}", events))
        return

    if args.events_action == "run":
        events = repo.by_run(args.run_id)
        if not events:
            console.print(f"[yellow]No events found for run id {args.run_id!r}[/yellow]")
            return
        console.print(_events_table(f"Run: {args.run_id}", events))
        return


def cmd_replay(args) -> None:
    cfg = load_config(args.config)

    if args.level == 4:
        try:
            level4_result = replay_schools_level4(cfg, args.identifier)
        except ReplayNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        console.print(f"L4 deterministic-stage replay: schools run {level4_result.run_id!r}  seed={level4_result.seed}  k={level4_result.k}")
        if level4_result.missing_cycle_ids:
            console.print(
                f"[yellow]{len(level4_result.missing_cycle_ids)} originally clustered cycle(s) no longer have "
                f"embeddings: {level4_result.missing_cycle_ids}[/yellow]"
            )
        console.print(f"Original partition:  {level4_result.original_partition}")
        console.print(f"Replayed partition:  {level4_result.replayed_partition}")
        if level4_result.identical_partition:
            console.print("[dim]Identical partition -- clustering reproduced exactly from the recorded seed/k.[/dim]")
        else:
            console.print("[yellow]Partition differs from what was originally recorded.[/yellow]")
        return

    if args.level == 5:
        if not args.variant_model:
            console.print("[red]--variant-model is required for --level 5[/red]")
            raise SystemExit(1)
        try:
            level5_result = replay_cycle_level5(cfg, int(args.identifier), args.variant_model)
        except ReplayNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        console.print(
            f"L5 comparative replay: cycle {level5_result.cycle_id}  "
            f"baseline={level5_result.baseline_model!r}  variant={level5_result.variant_model!r}"
        )
        if level5_result.diff:
            console.print(level5_result.diff)
        else:
            console.print("[dim]No textual difference between baseline and variant generations.[/dim]")
        return

    if args.level == 6:
        if not args.as_of:
            console.print("[red]--as-of is required for --level 6[/red]")
            raise SystemExit(1)
        try:
            level6_result = replay_cycle_against_historical_canon(cfg, int(args.identifier), args.as_of)
        except ReplayNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        console.print(
            f"L6 historical-canon replay: cycle {level6_result.cycle_id}  as_of={level6_result.as_of!r}\n"
            f"canon now: {level6_result.current_canon_cycle_ids}  "
            f"canon as of {level6_result.as_of!r}: {level6_result.historical_canon_cycle_ids}"
        )
        if level6_result.diff:
            console.print(level6_result.diff)
        else:
            console.print("[dim]No textual difference between the two canon states' generations.[/dim]")
        return

    if args.level == 2:
        try:
            level2_result = replay_cycle_level2(cfg, int(args.identifier))
        except ReplayNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        table = Table(title=f"L2 configuration replay: cycle {level2_result.cycle_id} (run {level2_result.run_id})")
        table.add_column("Field")
        table.add_column("Recorded")
        table.add_column("Current")
        table.add_column("Changed")
        for drift in level2_result.drifts:
            style = "yellow" if drift.changed else "dim"
            table.add_row(drift.field, str(drift.recorded), str(drift.current), "yes" if drift.changed else "no", style=style)
        console.print(table)
        if level2_result.any_drift:
            console.print("[yellow]Environment has drifted since this cycle ran.[/yellow]")
        else:
            console.print("[dim]No drift -- current environment matches what was recorded.[/dim]")
        return

    if args.level == 3:
        try:
            level3_result = replay_cycle_level3(cfg, int(args.identifier))
        except ReplayNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        console.print(
            f"L3 evidence replay: cycle {level3_result.cycle_id}  "
            f"verdict={level3_result.verdict}  final_score={level3_result.final_score}"
        )
        if not level3_result.sources:
            console.print("[dim]No cycle_sources rows recorded for this cycle.[/dim]")
        table = Table(title="Selected evidence (exact, reconstructed from cycle_sources)")
        table.add_column("Kind")
        table.add_column("Ref")
        table.add_column("Content")
        for source in level3_result.sources:
            excerpt = source.content if len(source.content) <= 120 else source.content[:117] + "..."
            table.add_row(source.source_kind, source.ref, excerpt)
        console.print(table)
        console.print(
            "[dim]Score decomposition (symbolic_density, doctrinal_enforcement, ...) is not stored -- "
            "only the rolled-up verdict/final_score persist, so it cannot be replayed here.[/dim]"
        )
        return

    try:
        level1_result = replay_cycle(cfg, int(args.identifier), model=args.model)
    except ReplayNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    console.print(f"Replayed cycle {level1_result.cycle_id} against model {level1_result.model!r}")
    if level1_result.diff:
        console.print(level1_result.diff)
    else:
        console.print("[dim]No textual difference from the original fragment.[/dim]")
    console.print(
        "[dim]Sampling nondeterminism means a diff here is expected -- replay audits the inputs "
        "(the exact prompt and model), not exact-output reproduction.[/dim]"
    )


def cmd_registry(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    if args.registry_action == "list":
        entries = list_registry(cfg, args.kind)
        if not entries:
            console.print(f"[dim]No {args.kind} registry entries recorded yet.[/dim]")
            return
        table = Table(title=f"{args.kind.capitalize()} registry")
        for column in entries[0]:
            table.add_column(column)
        for entry in entries:
            table.add_row(*(str(v) for v in entry.values()))
        console.print(table)
        return

    if args.registry_action == "show":
        shown_entry = show_registry_entry(cfg, args.kind, args.entry_id)
        if shown_entry is None:
            console.print(f"[red]No {args.kind} registry entry with id {args.entry_id!r}[/red]")
            raise SystemExit(1)
        for key, value in shown_entry.items():
            console.print(f"{key}: {value}")
        return


def cmd_provenance(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    if args.provenance_action == "backfill":
        report = backfill_provenance_edges(cfg)
        console.print(f"[green]Provenance edges recorded:[/green] {report.edges_added} (total now {report.edges_after})")
        if report.unresolved_book_refs:
            console.print(
                f"[yellow]{report.unresolved_book_refs} book evidence ref(s) could not be resolved to an exact "
                "chunk (ambiguous or missing canonical_ref) -- skipped, not guessed.[/yellow]"
            )
        if report.unattributed_canon_events:
            console.print(
                f"[yellow]{report.unattributed_canon_events} canon_events row(s) predate domain_events and "
                "could not be attributed to a specific council -- skipped, not guessed.[/yellow]"
            )
        return

    if args.provenance_action == "show":
        try:
            supply_chain = build_provenance_report_for_target(cfg, args.target_id)
        except LineageNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        table = Table(title=f"Epistemic supply chain: {supply_chain.object_type} {supply_chain.object_id}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Supporting evidence", str(supply_chain.supporting_evidence_count))
        table.add_row("Opposing evidence", str(supply_chain.opposing_evidence_count))
        table.add_row("Circular ancestry", "yes" if supply_chain.circular_ancestry else "no")
        table.add_row("Synthetic-dependency ratio", f"{supply_chain.synthetic_dependency_ratio:.2f}")
        table.add_row("Completeness score", f"{supply_chain.completeness_score:.2f}")
        table.add_row(
            "Model(s) used",
            ", ".join(supply_chain.model_registry_ids) if supply_chain.model_registry_ids else "[dim]none recorded[/dim]",
        )
        if supply_chain.weakest_link is not None:
            link = supply_chain.weakest_link
            table.add_row(
                "Weakest link",
                f"{link.source_type}:{link.source_id} --{link.relation_type}--> "
                f"{link.target_type}:{link.target_id} (confidence={link.confidence:.2f})",
            )
        else:
            table.add_row("Weakest link", "[dim]no ancestry edges recorded[/dim]")
        console.print(table)

        if supply_chain.missing_links:
            console.print("[yellow]Missing links:[/yellow]")
            for missing in supply_chain.missing_links:
                console.print(f"  - {missing}")
        if supply_chain.opposing_evidence_count == 0:
            console.print(
                "[dim]Opposing evidence is always 0 today -- no writer in this codebase produces a "
                "CONTRADICTS or evidence-level OPPOSES edge.[/dim]"
            )
        console.print("[dim]For the full up/down provenance graph, see `field-horizon lineage`.[/dim]")
        return


def cmd_serve(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    console.print(f"[green]Token file:[/green] {args.token_file} (created with chmod 600 if new)")
    console.print(f"[green]Serving on http://127.0.0.1:{args.port}[/green] (Ctrl-C to stop)")
    run_server(cfg, port=args.port, token_file=Path(args.token_file))


def cmd_export_godot(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    written = export_godot(cfg, Path(args.out))
    table = Table(title="Godot export")
    table.add_column("File")
    table.add_column("Path")
    for filename, path in written.items():
        table.add_row(filename, str(path))
    console.print(table)



def cmd_index(args) -> None:
    cfg = load_config(args.config)
    path = build_cycle_index(cfg)
    console.print(f"[green]Index rebuilt:[/green] {path}")

def cmd_ontology(args) -> None:
    cfg = load_config(args.config)
    path = export_pressure_report(cfg, limit=args.limit)
    console.print(f"[green]Ontology pressure exported:[/green] {path}")

def cmd_axiom_candidates(args) -> None:
    cfg = load_config(args.config)
    path = export_axiom_candidates(
        cfg,
        limit=args.limit,
        min_score=args.min_score,
        model=args.model,
    )
    console.print(f"[green]Axiom candidates exported:[/green] {path}")

def cmd_generate_axioms(args) -> None:
    cfg = load_config(args.config)

    path = export_generated_axioms(
        cfg,
        limit=args.limit,
        min_score=args.min_score,
        model=args.model,
        critic_model=args.critic_model,
    )

    console.print(f"[green]Generated axioms exported:[/green] {path}")

    if args.ingest:
        init_db(cfg.database)
        count = ingest_json_corpus(cfg)
        console.print(f"[green]Auto-ingested JSON entries:[/green] {count}")

def cmd_lineage(args) -> None:
    cfg = load_config(args.config)
    try:
        tree = build_lineage_tree(cfg, args.target_id)
    except LineageNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    stripped = args.target_id.strip()
    if stripped.lstrip("-").isdigit():
        add_provenance_graph_section(tree, cfg, OBJECT_CYCLE, int(stripped))
    else:
        add_provenance_graph_section(tree, cfg, OBJECT_JSON_ENTRY, stripped)

    console.print(tree)


def _canon_table(title: str, entries: list) -> Table:
    table = Table(title=title)
    table.add_column("Cycle")
    table.add_column("Verdict")
    table.add_column("Score", justify="right")
    table.add_column("Created")
    table.add_column("Query")
    for entry in entries:
        table.add_row(str(entry.cycle_id), entry.verdict, f"{entry.final_score:.3f}", entry.created_at, entry.query[:60])
    return table


def cmd_canon(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    if args.as_of_cycle is not None:
        entries = engine.canon_as_of_cycle(args.as_of_cycle)
        console.print(_canon_table(f"Active canon as of cycle {args.as_of_cycle}", entries))
        return

    if args.as_of_timestamp is not None:
        entries = engine.canon_as_of_timestamp(args.as_of_timestamp)
        console.print(_canon_table(f"Active canon as of {args.as_of_timestamp}", entries))
        return

    entries = engine.canon(verdict=args.verdict, include_retired=args.include_retired, limit=args.limit)
    console.print(_canon_table("Canon", entries))


def cmd_why_changed(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    result = FieldHorizonEngine(cfg).why_changed(args.cycle_id)

    if not result.transitions:
        console.print(f"[dim]No verdict transitions recorded for cycle {args.cycle_id} (predates Phase D, or never re-judged).[/dim]")
        return

    table = Table(title=f"Why cycle {args.cycle_id} changed")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Valid from")
    table.add_column("Event")
    table.add_column("Edge")
    for transition in result.transitions:
        from_label = f"{transition.from_state.verdict}" if transition.from_state is not None else "[dim](initial)[/dim]"
        to_label = f"{transition.to_state.verdict}{'' if transition.to_state.active else ' [red](inactive)[/red]'}"
        event_label = transition.event.event_type if transition.event is not None else "[dim]unresolved[/dim]"
        edge_label = f"{transition.edge.relation_type} from {transition.edge.source_type}:{transition.edge.source_id}" if transition.edge is not None else "[dim]unresolved[/dim]"
        table.add_row(from_label, to_label, transition.to_state.valid_from, event_label, edge_label)
    console.print(table)


def cmd_weather_as_of(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    readings = FieldHorizonEngine(cfg).weather_as_of(args.timestamp, scope=args.scope)

    if not readings:
        console.print(f"[dim]No weather readings recorded at or before {args.timestamp}.[/dim]")
        return

    table = Table(title=f"Weather as of {args.timestamp} ({args.scope})")
    table.add_column("Axis")
    table.add_column("Value", justify="right")
    for axis, value in readings.items():
        table.add_row(axis, f"{value:.3f}")
    console.print(table)


def cmd_school_membership_as_of(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    schools = FieldHorizonEngine(cfg).school_membership_as_of(args.timestamp)

    if not schools:
        console.print(f"[dim]No school generation was active as of {args.timestamp}.[/dim]")
        return

    table = Table(title=f"School membership as of {args.timestamp}")
    table.add_column("Id")
    table.add_column("Name")
    table.add_column("Members", justify="right")
    table.add_column("Lineage")
    for school in schools:
        lineage = f"formerly #{school.previous_school_id}" if school.previous_school_id else "-"
        table.add_row(str(school.id), school.name, str(school.member_count), lineage)
    console.print(table)


def cmd_backfill_temporal_canon(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    report = backfill_canon_temporal_states(cfg)
    console.print(
        f"[green]Temporal canon states written:[/green] {report.states_written} "
        f"({report.cycles_processed} cycle(s) processed)"
    )
    if report.unresolved_edges:
        console.print(
            f"[yellow]{report.unresolved_edges} transition(s) could not be linked to a provenance edge -- "
            "run `provenance backfill` first if you haven't yet.[/yellow]"
        )


def cmd_backfill_embeddings(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    count = backfill_canon_embeddings(cfg, limit=args.limit)
    console.print(f"[green]Embedded canon fragments:[/green] {count}")


def cmd_backfill_chunk_embeddings(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    count = backfill_chunk_embeddings(cfg, limit=args.limit)
    console.print(f"[green]Embedded chunks:[/green] {count}")


def cmd_backfill_axiom_embeddings(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    count = backfill_axiom_embeddings(cfg, limit=args.limit)
    console.print(f"[green]Embedded axioms:[/green] {count}")


def cmd_tag_concepts(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    count = tag_chunks(cfg, max_chunks=args.max_chunks)
    console.print(f"[green]Tagged chunks:[/green] {count}")


def cmd_fingerprint(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    if args.compare:
        source_id_a, source_id_b = (int(x) for x in args.compare)
        try:
            comparison = compare_fingerprints(cfg, source_id_a, source_id_b)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        console.print(f"Cosine similarity: {comparison.cosine_similarity:.4f}")
        console.print(f"Domain-distribution divergence: {comparison.domain_divergence:.4f}")
        return

    if args.source_id is None:
        console.print("[red]Provide a source_id, or --compare A B.[/red]")
        raise SystemExit(1)

    fp = FieldHorizonEngine(cfg).fingerprint(args.source_id)
    if fp is None:
        console.print(f"[red]Source {args.source_id} has no embedded chunks yet (run backfill-chunk-embeddings).[/red]")
        raise SystemExit(1)

    table = Table(title=f"Fingerprint: source {fp.source_id}")
    table.add_column("Domain")
    table.add_column("Share", justify="right")
    for domain, share in sorted(fp.domain_distribution.items(), key=lambda kv: -kv[1]):
        table.add_row(domain, f"{share:.1%}")
    console.print(table)

    console.print(f"Top motifs: {', '.join(fp.top_motifs) or '(none)'}")
    console.print(f"Chunks: {fp.chunk_count} | embedding_version: {fp.embedding_version}")


def _component_table(title: str, components: dict) -> Table:
    table = Table(title=title)
    table.add_column("Component")
    table.add_column("Raw", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Contribution", justify="right")
    for name, component in components.items():
        table.add_row(name, f"{component.raw:.4f}", f"{component.weight:.2f}", f"{component.contribution:.4f}")
    return table


def cmd_retrieve(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    if args.plan:
        result = FieldHorizonEngine(cfg).retrieve_planned(args.query, limit=args.limit, as_of=args.as_of)
        plan = result.plan

        plan_table = Table(title=f"Retrieval plan: {args.query!r}")
        plan_table.add_column("Strategy")
        plan_table.add_column("Selected")
        plan_table.add_column("Why")
        for strategy in plan.strategies_available:
            selected = "yes" if strategy.name in plan.strategies_selected else "no"
            console_style = "green" if strategy.name in plan.strategies_selected else "dim"
            plan_table.add_row(strategy.name, selected, strategy.reason, style=console_style)
        console.print(plan_table)

        console.print(f"[dim]Constraints applied:[/dim] {', '.join(result.constraints_applied)}")
        if result.excluded_chunk_candidates or result.excluded_canon_candidates:
            console.print("[yellow]Excluded:[/yellow]")
            for excluded_chunk in result.excluded_chunk_candidates:
                console.print(f"  - chunk {excluded_chunk.chunk_id} ({excluded_chunk.canonical_ref}): {excluded_chunk.exclusion_reason}")
            for excluded_canon in result.excluded_canon_candidates:
                console.print(f"  - cycle {excluded_canon.cycle_id}: {excluded_canon.exclusion_reason}")

        if not result.chunk_candidates and not result.canon_candidates:
            console.print("[yellow]No candidates found.[/yellow]")
            return

        if result.chunk_candidates:
            chunk_table = Table(title="Chunk candidates")
            chunk_table.add_column("#", justify="right")
            chunk_table.add_column("Score", justify="right")
            chunk_table.add_column("Source")
            chunk_table.add_column("Ref")
            for rank, chunk_candidate in enumerate(result.chunk_candidates, start=1):
                chunk_table.add_row(str(rank), f"{chunk_candidate.score:.4f}", chunk_candidate.source_title, chunk_candidate.canonical_ref)
            console.print(chunk_table)
            if args.explain:
                for rank, chunk_candidate in enumerate(result.chunk_candidates, start=1):
                    console.print(_component_table(f"#{rank}  {chunk_candidate.canonical_ref}  (score {chunk_candidate.score:.4f})", chunk_candidate.components))

        if result.canon_candidates:
            canon_table = Table(title="Canon candidates")
            canon_table.add_column("#", justify="right")
            canon_table.add_column("Score", justify="right")
            canon_table.add_column("Cycle")
            canon_table.add_column("Query")
            for rank, canon_candidate in enumerate(result.canon_candidates, start=1):
                canon_table.add_row(str(rank), f"{canon_candidate.score:.4f}", str(canon_candidate.cycle_id), canon_candidate.query[:60])
            console.print(canon_table)
            if args.explain:
                for rank, canon_candidate in enumerate(result.canon_candidates, start=1):
                    console.print(_component_table(f"#{rank}  cycle {canon_candidate.cycle_id}  (score {canon_candidate.score:.4f})", canon_candidate.components))
        return

    results = FieldHorizonEngine(cfg).retrieve(args.query, limit=args.limit, explain=args.explain)

    if not results:
        console.print("[yellow]No candidates found.[/yellow]")
        return

    if args.explain:
        for rank, candidate in enumerate(results, start=1):
            table = Table(title=f"#{rank}  {candidate.source_title}  (score {candidate.score:.4f})")
            table.add_column("Component")
            table.add_column("Raw", justify="right")
            table.add_column("Weight", justify="right")
            table.add_column("Contribution", justify="right")
            for name, component in candidate.components.items():
                table.add_row(name, f"{component.raw:.4f}", f"{component.weight:.2f}", f"{component.contribution:.4f}")
            console.print(table)
            snippet = candidate.content[:220] + ("..." if len(candidate.content) > 220 else "")
            console.print(f"[dim]{candidate.canonical_ref}[/dim]\n{snippet}\n")
    else:
        table = Table(title=f"retrieve: {args.query!r}")
        table.add_column("#", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Source")
        table.add_column("Ref")
        for rank, candidate in enumerate(results, start=1):
            table.add_row(str(rank), f"{candidate.score:.4f}", candidate.source_title, candidate.canonical_ref)
        console.print(table)


def cmd_ingest_manifest(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    try:
        results = ingest_from_manifest(cfg)
    except ManifestError as exc:
        console.print(f"[red]Manifest error:[/red] {exc}")
        raise SystemExit(1) from exc

    table = Table(title="ingest-manifest")
    table.add_column("Manifest id")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Chunks", justify="right")

    status_style = {
        "ingested": "[green]ingested[/green]",
        "adopted_existing": "[cyan]adopted existing[/cyan]",
        "already_ingested": "[dim]already ingested[/dim]",
        "missing_file": "[red]missing file[/red]",
    }

    for result in results:
        table.add_row(
            result.manifest_id,
            result.title,
            status_style.get(result.status, result.status),
            str(result.chunk_count) if result.status == "ingested" else "-",
        )

    console.print(table)

    ingested = sum(1 for r in results if r.status == "ingested")
    adopted = sum(1 for r in results if r.status == "adopted_existing")
    missing = sum(1 for r in results if r.status == "missing_file")
    already = len(results) - ingested - adopted - missing
    console.print(f"Ingested: {ingested} | Adopted existing: {adopted} | Already ingested: {already} | Missing files: {missing}")


def cmd_backfill_chunk_offsets(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    found, missed = backfill_chunk_offsets(cfg)
    console.print(f"[green]Resolved offsets:[/green] {found}")
    if missed:
        console.print(f"[yellow]Missed (source changed or unreadable):[/yellow] {missed}")


def cmd_council(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    report = FieldHorizonEngine(cfg).run_council(
        sample=args.sample,
        min_age_days=args.min_age_days,
        audit_canon=args.audit_canon,
    )

    if not report.heresy_trials and not report.canon_audits:
        console.print("[yellow]No candidates met the council's criteria this session.[/yellow]")
        console.print(f"Report: {report.report_path}")
        return

    if report.heresy_trials:
        table = Table(title=f"Council {report.council_id:04d}: heresy trials")
        table.add_column("Cycle")
        table.add_column("Old verdict")
        table.add_column("New verdict")
        table.add_column("Rehabilitated?")

        for trial in report.heresy_trials:
            table.add_row(
                f"{trial.cycle_id} ({trial.old_score:.3f} -> {trial.new_score:.3f})",
                trial.old_verdict,
                trial.new_verdict,
                "[green]yes[/green]" if trial.rehabilitated else "[red]no[/red]",
            )

        console.print(table)

    if report.canon_audits:
        table = Table(title=f"Council {report.council_id:04d}: canon audit")
        table.add_column("Cycle")
        table.add_column("Old score")
        table.add_column("New verdict")
        table.add_column("Retired?")

        for audit in report.canon_audits:
            table.add_row(
                str(audit.cycle_id),
                f"{audit.old_score:.3f}",
                f"{audit.new_verdict} ({audit.new_score:.3f})",
                "[red]yes[/red]" if audit.retired else "[green]no[/green]",
            )

        console.print(table)

    console.print(f"Examined: {report.examined} | Overturned: {report.overturned}")
    console.print(f"[green]Report:[/green] {report.report_path}")


def cmd_schools(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    k: str | int = "auto" if args.k == "auto" else int(args.k)
    results = run_schools(cfg, k=k, seed=args.seed)

    if not results:
        console.print("[yellow]Not enough embedded canon to cluster into schools yet.[/yellow]")
        return

    table = Table(title="Schools of thought")
    table.add_column("Id")
    table.add_column("Name")
    table.add_column("Members", justify="right")
    table.add_column("Lineage")
    for school in results:
        lineage = f"formerly #{school.previous_school_id}" if school.previous_school_id else "-"
        table.add_row(str(school.school_id), school.name, str(len(school.member_cycle_ids)), lineage)
    console.print(table)

    for school in results:
        console.print(Panel(school.summary, title=f"#{school.school_id}  {school.name}", border_style="cyan"))


def cmd_backfill_weather(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)
    count = backfill_canon_weather(cfg)
    console.print(f"[green]Backfilled canon weather readings:[/green] {count}")


def cmd_weather(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    if args.html:
        path = write_weather_html(cfg)
        console.print(f"[green]Weather dashboard written:[/green] {path}")
        return

    render_weather_tui(cfg, console=console)


def cmd_dream(args) -> None:
    cfg = load_config(args.config)
    init_db(cfg.database)

    run = run_dream(
        cfg,
        budget_cycles=args.budget_cycles,
        budget_minutes=args.budget_minutes,
        seed=args.seed,
        model=args.model,
        critic_model=args.critic_model,
    )

    console.print(f"[green]Dream complete:[/green] {run.iterations} iteration(s) between {run.started_at} and {run.finished_at}")
    console.print(f"Ledger: {run.ledger_path}")
    console.print(f"Summary: {run.summary_path}")
    if run.proposals_written:
        console.print(f"[yellow]Ontology proposals written:[/yellow] {len(run.proposals_written)}")
        for path in run.proposals_written:
            console.print(f"  {path}")
    console.print(Panel(run.summary_path.read_text(encoding="utf-8"), title="Dream Summary", border_style="magenta"))


def cmd_proposals(args) -> None:
    cfg = load_config(args.config)

    if args.proposals_action == "list":
        summaries = list_proposals(cfg)
        if not summaries:
            console.print("[dim]No pending proposals.[/dim]")
            return
        table = Table(title="Ontology Proposals")
        table.add_column("Id")
        table.add_column("Domain")
        table.add_column("Motif")
        table.add_column("Count", justify="right")
        for s in summaries:
            table.add_row(f"{s.number:04d}", s.name, s.motif, str(s.count))
        console.print(table)
        return

    if args.proposals_action == "show":
        try:
            data = show_proposal(cfg, args.number)
        except ProposalError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc
        console.print(yaml_dump_for_display(data))
        return

    if args.proposals_action == "apply":
        try:
            dest = apply_proposal(cfg, args.number)
        except ProposalError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc
        console.print(f"[green]Applied and committed:[/green] {dest}")
        return

    if args.proposals_action == "reject":
        try:
            dest = reject_proposal(cfg, args.number)
        except ProposalError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc
        console.print(f"[yellow]Rejected:[/yellow] {dest}")
        return


def yaml_dump_for_display(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def cmd_rust_pressure(args) -> None:
    cfg = load_config(args.config)
    path = export_rust_pressure_report(
        cfg,
        input_path=Path(args.input),
        limit=args.limit,
    )
    console.print(f"[green]Rust pressure report exported:[/green] {path}")

def cmd_rust_pressure_all(args) -> None:
    cfg = load_config(args.config)
    path = export_rust_pressure_report_all(
        cfg,
        limit=args.limit,
    )
    console.print(f"[green]Rust full corpus pressure report exported:[/green] {path}")
        
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="field-horizon", description="Field Horizon v3.2 canonical JSON engine")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("init")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ingest")
    s.add_argument("--all", action="store_true")
    s.add_argument("--books", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--manifestos", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stats", help="Engine-wide counters via the platform facade.")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("cycle")
    s.add_argument("--query", required=True)
    s.add_argument("--model", default=None)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--auto-rewrite", action="store_true")
    s.add_argument("--critic-model", default=None)
    s.add_argument("--rewrite-model", default=None)
    s.add_argument("--json-domain", default=None)
    s.set_defaults(func=cmd_cycle)

    s = sub.add_parser("multi-cycle")

    s.add_argument("--query", required=True)

    s.add_argument("--model", default=None)
    s.add_argument("--interpreter-model", default=None)

    s.add_argument("--agent-model", default=None)
    s.add_argument("--synthesizer-model", default=None)

    s.add_argument("--critic-model", default=None)
    s.add_argument("--rewrite-model", default=None)

    s.add_argument("--dry-run", action="store_true")

    s.add_argument(
        "--auto-rewrite",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    s.add_argument("--json-domain", default=None)

    s.add_argument(
        "--live",
        action="store_true",
        help="Rich live view: four agent panels filling in as they complete, then synthesis.",
    )

    s.set_defaults(func=cmd_multi_cycle)

    s = sub.add_parser("export-codex")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("export", help="Export platform payloads for external consumers.")
    export_sub = s.add_subparsers(dest="export_target", required=True)
    es = export_sub.add_parser("godot", help="Write factions/doctrines/contradictions/weather/beliefs JSON.")
    es.add_argument("--out", default="exports/godot/")
    es.set_defaults(func=cmd_export_godot)

    s = sub.add_parser("serve", help="Local-only platform API (127.0.0.1, bearer-token authenticated).")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--token-file", default=".fh_token")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("replay", help="Re-execute a cycle's exact stored prompt and diff old vs new fragment.")
    s.add_argument(
        "identifier",
        help="A cycle id (levels 1, 2, 3, 5) or a schools run_id (level 4 -- see `events run`/`registry` to find one).",
    )
    s.add_argument("--model", default=None, help="Override the model string (default: the cycle's own recorded model). Level 1 only.")
    s.add_argument("--variant-model", default=None, help="The model to compare against the cycle's original. Level 5 only, required.")
    s.add_argument("--as-of", default=None, help="Timestamp to reconstruct canon as of. Level 6 only, required.")
    s.add_argument(
        "--level", type=int, choices=(1, 2, 3, 4, 5, 6), default=1,
        help="1: re-run the stored prompt (default). 2: compare recorded vs current environment fingerprints. "
        "3: reconstruct the exact selected-evidence set from cycle_sources. "
        "4: re-run schools clustering from its recorded seed/k and compare partitions. "
        "5: diff a fresh baseline generation against a fresh --variant-model generation. "
        "6: diff a fresh generation under canon now against canon as of --as-of (Phase D).",
    )
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("registry", help="Inspect the model/prompt/embedding registries (Implementation Brief III, Phase B).")
    registry_sub = s.add_subparsers(dest="registry_action", required=True)

    rs = registry_sub.add_parser("list", help="List registered entries of one kind.")
    rs.add_argument("kind", choices=("model", "prompt", "embedding"))
    rs.set_defaults(func=cmd_registry)

    rs = registry_sub.add_parser("show", help="Full detail of one registry entry, by its id.")
    rs.add_argument("kind", choices=("model", "prompt", "embedding"))
    rs.add_argument("entry_id")
    rs.set_defaults(func=cmd_registry)

    s = sub.add_parser("provenance", help="The provenance graph (Implementation Brief III, Phase C).")
    provenance_sub = s.add_subparsers(dest="provenance_action", required=True)

    ps = provenance_sub.add_parser(
        "backfill",
        help="One-shot historical reconstruction of provenance edges from existing cycles/schools/canon_events/json_entries.",
    )
    ps.set_defaults(func=cmd_provenance)

    ps = provenance_sub.add_parser(
        "show",
        help="The epistemic supply chain for one object: a cycle id, or a json_entries axiom id.",
    )
    ps.add_argument("target_id", help="A cycle id (numeric) or a json_entries axiom id -- same dispatch as `lineage`.")
    ps.set_defaults(func=cmd_provenance)

    s = sub.add_parser("events", help="Inspect the unified domain_events operational event stream.")
    events_sub = s.add_subparsers(dest="events_action", required=True)

    evs = events_sub.add_parser("list", help="Most recently recorded events.")
    evs.add_argument("--limit", type=int, default=50)
    evs.set_defaults(func=cmd_events)

    evs = events_sub.add_parser("show", help="Full detail of one event, by its event_id.")
    evs.add_argument("event_id")
    evs.set_defaults(func=cmd_events)

    evs = events_sub.add_parser("trace", help="Every event sharing one correlation_id (one operation's causal chain).")
    evs.add_argument("correlation_id")
    evs.set_defaults(func=cmd_events)

    evs = events_sub.add_parser("run", help="Every event sharing one run_id.")
    evs.add_argument("run_id")
    evs.set_defaults(func=cmd_events)

    s = sub.add_parser("index")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("ontology")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_ontology)

    s = sub.add_parser("axiom-candidates")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--min-score", type=float, default=1.0)
    s.add_argument("--model", default=None)
    s.set_defaults(func=cmd_axiom_candidates)

    s = sub.add_parser("generate-axioms")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--min-score", type=float, default=1.0)
    s.add_argument("--model", default=None)
    s.add_argument("--critic-model", default=None)
    s.add_argument("--ingest", action="store_true")
    s.set_defaults(func=cmd_generate_axioms)

    s = sub.add_parser("rust-pressure")
    s.add_argument("--input", required=True)
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_rust_pressure)

    s = sub.add_parser("rust-pressure-all")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_rust_pressure_all)

    s = sub.add_parser("lineage")
    s.add_argument("target_id", help="A json_entries axiom id, or a numeric cycles.id for canon genealogy.")
    s.set_defaults(func=cmd_lineage)

    s = sub.add_parser("canon", help="List canon, now or reconstructed as of a historical point (Implementation Brief III, Phase D).")
    s.add_argument("--verdict", default="CANON")
    s.add_argument("--include-retired", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--as-of-cycle", type=int, default=None, help="Reconstruct active canon as of the moment this cycle id was created.")
    s.add_argument("--as-of-timestamp", default=None, help="Reconstruct active canon as of an explicit timestamp (valid-time travel).")
    s.set_defaults(func=cmd_canon)

    s = sub.add_parser("why-changed", help="Resolve a cycle's verdict transitions to the real event and provenance edge that caused each (Implementation Brief III, Phase D).")
    s.add_argument("cycle_id", type=int)
    s.set_defaults(func=cmd_why_changed)

    s = sub.add_parser("weather-as-of", help="Doctrinal weather reconstructed as of a historical timestamp (Implementation Brief III, Phase D).")
    s.add_argument("timestamp")
    s.add_argument("--scope", default="canon", choices=("canon", "surface"))
    s.set_defaults(func=cmd_weather_as_of)

    s = sub.add_parser("school-membership-as-of", help="The school-of-thought generation active as of a historical timestamp (Implementation Brief III, Phase D).")
    s.add_argument("timestamp")
    s.set_defaults(func=cmd_school_membership_as_of)

    s = sub.add_parser("backfill-temporal-canon", help="One-shot historical reconstruction of canon_temporal_states from existing cycles/canon_events (Implementation Brief III, Phase D).")
    s.set_defaults(func=cmd_backfill_temporal_canon)

    s = sub.add_parser("backfill-embeddings")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_backfill_embeddings)

    s = sub.add_parser("backfill-chunk-embeddings")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_backfill_chunk_embeddings)

    s = sub.add_parser("backfill-chunk-offsets")
    s.set_defaults(func=cmd_backfill_chunk_offsets)

    s = sub.add_parser("backfill-axiom-embeddings")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_backfill_axiom_embeddings)

    s = sub.add_parser("ingest-manifest", help="Ingest exactly the files listed in data/sources.yaml (curation as code).")
    s.set_defaults(func=cmd_ingest_manifest)

    s = sub.add_parser("tag-concepts", help="Tag untagged chunks with ontology domains, entities, and motifs.")
    s.add_argument("--max-chunks", type=int, default=None)
    s.set_defaults(func=cmd_tag_concepts)

    s = sub.add_parser("fingerprint", help="Semantic fingerprint of a source, or --compare two sources.")
    s.add_argument("source_id", type=int, nargs="?", help="Source id (omit when using --compare).")
    s.add_argument("--compare", nargs=2, metavar=("A", "B"), help="Compare two source ids instead of showing one.")
    s.set_defaults(func=cmd_fingerprint)

    s = sub.add_parser("retrieve", help="Hybrid retrieval v2 over book/manifesto chunks.")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--explain", action="store_true", help="Show each candidate's score decomposition.")
    s.add_argument(
        "--plan", action="store_true",
        help="Retrieval planner v3 (Implementation Brief III, Phase E): classify the query, select and run "
        "strategies beyond v2, enforce retrieval_policy, and show the plan itself.",
    )
    s.add_argument("--as-of", default=None, help="Timestamp for TEMPORAL strategy: filters CANON_GENEALOGY candidates. Only meaningful with --plan.")
    s.set_defaults(func=cmd_retrieve)

    s = sub.add_parser("schools", help="Cluster active canon into schools of thought (descriptive only).")
    s.add_argument("--k", default="auto", help="Number of clusters, or 'auto' for silhouette-based selection.")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_schools)

    s = sub.add_parser("backfill-weather", help="Reconstruct canon-scope weather history across existing canon.")
    s.set_defaults(func=cmd_backfill_weather)

    s = sub.add_parser("weather", help="Doctrinal weather dashboard (read-only analytics).")
    s.add_argument("--html", action="store_true", help="Write a static self-contained outputs/weather.html instead of the TUI.")
    s.set_defaults(func=cmd_weather)

    s = sub.add_parser("dream", help="Foreground, budget-capped exploration: mutate, evaluate, propose.")
    s.add_argument("--budget-cycles", type=int, required=True)
    s.add_argument("--budget-minutes", type=float, required=True)
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--model", default=None)
    s.add_argument("--critic-model", default=None)
    s.set_defaults(func=cmd_dream)

    s = sub.add_parser("proposals", help="Manage ontology proposals emitted by dream runs.")
    proposals_sub = s.add_subparsers(dest="proposals_action", required=True)

    ps = proposals_sub.add_parser("list")
    ps.set_defaults(func=cmd_proposals)

    ps = proposals_sub.add_parser("show")
    ps.add_argument("number", type=int)
    ps.set_defaults(func=cmd_proposals)

    ps = proposals_sub.add_parser("apply")
    ps.add_argument("number", type=int)
    ps.set_defaults(func=cmd_proposals)

    ps = proposals_sub.add_parser("reject")
    ps.add_argument("number", type=int)
    ps.set_defaults(func=cmd_proposals)

    # Autonomous Open Corpus Harvester. Mounted from its own module so
    # that ~15 verbs and their arguments do not add another 200 lines
    # here, and so a corpus-side import error can never break the rest
    # of the CLI (the import is local to this call).
    from .corpus.cli import add_corpus_parser

    add_corpus_parser(sub)

    s = sub.add_parser("council")
    s.add_argument("--sample", type=int, default=5)
    s.add_argument("--min-age-days", type=int, default=7)
    s.add_argument(
        "--audit-canon",
        action="store_true",
        help="Also re-evaluate the oldest active canon; retire what no longer clears the canon threshold.",
    )
    s.set_defaults(func=cmd_council)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
