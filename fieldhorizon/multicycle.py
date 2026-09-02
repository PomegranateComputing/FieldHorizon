from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime

from .agents import AgentOutput, run_agents
from .canon import load_canon_fragments, load_motif_counts, record_canon_ngrams
from .config import AppConfig
from .critic import critique_response
from .db import connect
from .embeddings import embed_and_store_fragment
from .evaluate import evaluate_cycle, evaluation_to_markdown
from .events import (
    ACTOR_CLI,
    AGGREGATE_CYCLE,
    EVT_AGENT_COMPLETED,
    EVT_CYCLE_COMPLETED,
    EVT_CYCLE_FAILED,
    EVT_CYCLE_STARTED,
    EVT_EVALUATION_COMPLETED,
    EVT_RETRIEVAL_COMPLETED,
    OperationEmitter,
)
from .fragments import extract_field_fragment
from .genealogy import EVENT_PROMOTED, parent_cycle_ids_for, record_canon_event
from .indexer import build_cycle_index
from .interpreter import interpret_query, interpretation_to_prompt
from .manifests import record_operation_manifest
from .prompting import build_prompt
from .provenance import record_selected_by_edges, record_supports_edges
from .retrieval import search_books, search_json
from .rewrite import rewrite_response
from .rustcore import (
    export_rust_pressure_report_all,
    filter_rust_report_by_refs,
    rust_report_to_prompt,
)
from .synthesis import (
    agent_outputs_to_text,
    build_structured_response,
    build_synthesis_prompt,
    generate_fragment,
)
from .temporal import TemporalCanonRepository
from .verdicts import canon_dir_for_verdict, should_rewrite
from .weather import record_cycle_weather

logger = logging.getLogger(__name__)


def row_to_dict(row) -> dict:
    # sqlite3.Row iterates its *values*, not its column names -- .keys()
    # is required here (ruff's SIM118 doesn't know this isn't a plain dict).
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


def run_multi_cycle(
    cfg: AppConfig,
    query: str,
    model: str | None = None,
    agent_model: str | None = None,
    synthesizer_model: str | None = None,
    critic_model: str | None = None,
    rewrite_model: str | None = None,
    interpreter_model: str | None = None,
    dry_run: bool = False,
    auto_rewrite: bool = True,
    json_domain: str | None = None,
    on_agent_done: Callable[[AgentOutput], None] | None = None,
    on_synthesis_done: Callable[[str], None] | None = None,
    actor: str = ACTOR_CLI,
    correlation_id: str | None = None,
) -> int:
    """
    `on_agent_done`/`on_synthesis_done`, if given, are called synchronously
    as each agent completes and once the synthesized fragment is ready --
    the hook a live Rich view of an in-progress multi-cycle (review §9)
    renders from. Neither fires in dry-run mode, since no agents or
    synthesizer actually run.

    `correlation_id`, if given, lets a caller (Phase UI-4 item 4's
    POST /multi-cycle route) pre-mint the run_id and start watching
    GET /events/stream?run_id= before this synchronous call returns.
    """
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_CYCLE, correlation_id=correlation_id)

    try:
        return _run_multi_cycle_body(
            cfg, query, model, agent_model, synthesizer_model, critic_model, rewrite_model,
            interpreter_model, dry_run, auto_rewrite, json_domain, on_agent_done, on_synthesis_done, emitter,
        )
    except Exception as exc:
        emitter.emit(EVT_CYCLE_FAILED, payload={"error_type": type(exc).__name__, "error_message": str(exc)})
        raise


def _run_multi_cycle_body(
    cfg: AppConfig,
    query: str,
    model: str | None,
    agent_model: str | None,
    synthesizer_model: str | None,
    critic_model: str | None,
    rewrite_model: str | None,
    interpreter_model: str | None,
    dry_run: bool,
    auto_rewrite: bool,
    json_domain: str | None,
    on_agent_done: Callable[[AgentOutput], None] | None,
    on_synthesis_done: Callable[[str], None] | None,
    emitter: OperationEmitter,
) -> int:
    emitter.emit(EVT_CYCLE_STARTED, payload={"query": query, "model": model or cfg.default_model, "dry_run": dry_run})

    interpretation = interpret_query(cfg, query, model=interpreter_model)
    retrieval_query = interpretation.get("retrieval_query") or query

    book_rows = search_books(cfg, retrieval_query)
    json_rows = search_json(cfg, retrieval_query, domain=json_domain)
    canon_rows = load_canon_fragments(cfg, query=retrieval_query, limit=3)
    motif_counts = load_motif_counts(cfg)
    parent_cycle_ids = parent_cycle_ids_for(canon_rows)

    emitter.emit(
        EVT_RETRIEVAL_COMPLETED,
        payload={"book_count": len(book_rows), "json_count": len(json_rows), "canon_count": len(canon_rows)},
    )

    base_prompt = build_prompt(cfg, query, book_rows, json_rows, canon_rows)

    source_refs = [row["canonical_ref"] for row in book_rows]
    json_refs = [row["id"] for row in json_rows]
    canon_refs = [row["ref"] for row in canon_rows]
    json_dicts = [row_to_dict(row) for row in json_rows]
    json_axioms = [
        f"{row.get('category', 'axiom')} :: {row.get('statement', '')}".strip()
        for row in json_dicts[:8]
    ]

    rust_pressure_failure: str | None = None

    try:
        rust_report_path = export_rust_pressure_report_all(cfg, limit=25)
        rust_report = json.loads(rust_report_path.read_text(encoding="utf-8"))
        rust_report = filter_rust_report_by_refs(rust_report, json_refs)
        rust_pressure_text = rust_report_to_prompt(rust_report, limit=12)
    except Exception as exc:
        rust_pressure_text = f"[rust pressure unavailable: {exc}]"
        rust_pressure_failure = f"{type(exc).__name__}: {exc}"
        logger.warning("Rust pressure core unavailable, degrading synthesis: %s", rust_pressure_failure)

    if dry_run:
        agent_outputs: list[AgentOutput] = []
        synthesis_prompt = "[DRY RUN]\n\n" + base_prompt
        response = build_structured_response(
            fragment_text=synthesis_prompt,
            query=query,
            source_refs=source_refs,
            json_refs=json_refs,
            canon_refs=canon_refs,
            interpretation=interpretation,
            retrieval_query=retrieval_query,
            dominant_traditions=["multi_agent", "recursive_canon"],
            tags=["multi_agent", "conflict", "synthesis"],
        )
    else:
        def _emitting_on_agent_done(output: AgentOutput) -> None:
            emitter.emit(
                EVT_AGENT_COMPLETED, payload={"agent_name": output.name, "content_length": len(output.content or "")}
            )
            if on_agent_done is not None:
                on_agent_done(output)

        agent_outputs = run_agents(
            cfg, base_prompt, model=agent_model or model, on_agent_done=_emitting_on_agent_done
        )

        synthesis_prompt = build_synthesis_prompt(
            base_prompt,
            agent_outputs,
            json_domain=json_domain,
            rust_pressure_text=rust_pressure_text,
            interpretation_text=interpretation_to_prompt(interpretation),
            query=query,
            source_refs=source_refs,
            json_refs=json_refs,
            json_axioms=json_axioms,
        )

        fragment = generate_fragment(
            cfg,
            synthesis_prompt,
            model=synthesizer_model or model,
        )

        if on_synthesis_done is not None:
            on_synthesis_done(fragment)

        response = build_structured_response(
            fragment_text=fragment,
            query=query,
            source_refs=source_refs,
            json_refs=json_refs,
            canon_refs=canon_refs,
            interpretation=interpretation,
            retrieval_query=retrieval_query,
            dominant_traditions=["multi_agent", "recursive_canon"],
            tags=["multi_agent", "conflict", "synthesis"],
        )

    evaluation = evaluate_cycle(response, json_dicts, motif_counts=motif_counts, cfg=cfg)
    evaluation_md = evaluation_to_markdown(evaluation)

    critique = ""
    rewritten_response = ""
    rewritten_evaluation_md = ""

    final_response = response
    final_evaluation = evaluation
    final_evaluation_md = evaluation_md

    if auto_rewrite and not dry_run and should_rewrite(evaluation):
        critique = critique_response(
            cfg=cfg,
            original_prompt=synthesis_prompt,
            response=response,
            model=critic_model or model,
        )

        raw_rewritten_response = rewrite_response(
            cfg=cfg,
            original_prompt=synthesis_prompt,
            original_response=response,
            critique=critique,
            model=rewrite_model or model,
            json_rows=json_dicts,
        )

        rewritten_fragment = extract_field_fragment(raw_rewritten_response)
        rewritten_response = build_structured_response(
            fragment_text=rewritten_fragment,
            query=query,
            source_refs=source_refs,
            json_refs=json_refs,
            canon_refs=canon_refs,
            interpretation=interpretation,
            retrieval_query=retrieval_query,
            dominant_traditions=["multi_agent", "recursive_canon"],
            tags=["multi_agent", "conflict", "synthesis"],
        )

        rewritten_evaluation = evaluate_cycle(
            rewritten_response,
            json_dicts,
            motif_counts=motif_counts,
            cfg=cfg,
        )
        rewritten_evaluation_md = evaluation_to_markdown(rewritten_evaluation)

        if rewritten_evaluation.final_score >= evaluation.final_score:
            final_response = rewritten_response
            final_evaluation = rewritten_evaluation
            final_evaluation_md = rewritten_evaluation_md

    emitter.emit(
        EVT_EVALUATION_COMPLETED,
        payload={
            "verdict": final_evaluation.verdict,
            "final_score": final_evaluation.final_score,
            "was_rewritten": final_response is not response,
        },
    )

    cfg.logs.mkdir(parents=True, exist_ok=True)
    cfg.outputs.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = cfg.logs / f"multi_cycle_{stamp}.md"

    full_log = (
        f"# Field Horizon Multi-Agent Cycle {stamp}\n\n"
        f"## Query\n{query}\n\n"
        f"## Semantic Interpretation\n```json\n{interpretation_to_prompt(interpretation)}\n```\n\n"
        f"## Retrieval Query\n{retrieval_query}\n\n"
    )

    if rust_pressure_failure:
        full_log += (
            f"## Rust Pressure\n**DEGRADED: rust pressure core unavailable.** "
            f"{rust_pressure_failure}\n\n"
        )

    full_log += f"## Base Prompt\n```text\n{base_prompt}\n```\n\n"

    if agent_outputs:
        full_log += f"## Agent Memoranda\n```text\n{agent_outputs_to_text(agent_outputs)}\n```\n\n"

    full_log += (
        f"## Synthesis Prompt\n```text\n{synthesis_prompt}\n```\n\n"
        f"## Initial Synthesis\n{response}\n\n"
        f"{evaluation_md}\n"
    )

    if critique:
        full_log += (
            f"\n## Critique\n{critique}\n\n"
            f"## Rewritten Synthesis\n{rewritten_response}\n\n"
            f"{rewritten_evaluation_md}\n"
        )

    full_log += (
        f"\n## Final Selected Response\n{final_response}\n\n"
        f"{final_evaluation_md}\n"
    )

    log_path.write_text(full_log, encoding="utf-8")

    final_fragment = extract_field_fragment(final_response)

    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query,
                model or cfg.default_model,
                synthesis_prompt,
                final_response,
                1 if dry_run else 0,
                final_evaluation.verdict,
                final_evaluation.final_score,
                final_fragment,
                json.dumps(parent_cycle_ids) if parent_cycle_ids else None,
            ),
        )
        assert cur.lastrowid is not None
        cycle_id = int(cur.lastrowid)

        for row in book_rows:
            conn.execute(
                "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, ?, ?, ?)",
                (cycle_id, "book", row["canonical_ref"], row["content"]),
            )

        for row in json_rows:
            conn.execute(
                "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, ?, ?, ?)",
                (cycle_id, "json", row["id"], row["statement"]),
            )

        for canon_row in canon_rows:
            conn.execute(
                "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, ?, ?, ?)",
                (cycle_id, "canon", canon_row["ref"], canon_row["content"]),
            )

        for out in agent_outputs:
            conn.execute(
                "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, ?, ?, ?)",
                (cycle_id, "agent", out.name, out.content),
            )

        conn.commit()

    try:
        record_selected_by_edges(cfg, cycle_id, parent_cycle_ids, provenance_ref=emitter.correlation_id)
        record_supports_edges(
            cfg,
            cycle_id,
            [("book", row["canonical_ref"]) for row in book_rows] + [("json", row["id"]) for row in json_rows],
            provenance_ref=emitter.correlation_id,
        )
    except Exception as exc:
        # Best-effort, matching cycle.py's own provenance-edge recording.
        logger.warning("Provenance edge recording failed for cycle %d: %s", cycle_id, exc)

    if not dry_run:
        target_dir = canon_dir_for_verdict(cfg, final_evaluation.verdict)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"cycle_{cycle_id}.md"
        target_path.write_text(full_log, encoding="utf-8")
        build_cycle_index(cfg)

        if final_evaluation.verdict == "CANON" and final_fragment:
            embed_and_store_fragment(cfg, cycle_id, final_fragment)
            record_canon_ngrams(cfg, cycle_id, final_fragment)
            # record_canon_event cross-emits FragmentPromoted itself; adopt()
            # keeps this emitter's causation chain pointing at it (see cycle.py).
            domain_event = record_canon_event(
                cfg, cycle_id, EVENT_PROMOTED,
                {"verdict": final_evaluation.verdict, "final_score": final_evaluation.final_score},
                actor=emitter.actor, correlation_id=emitter.correlation_id, causation_id=emitter.last_event_id,
            )
            if domain_event is not None:
                emitter.adopt(domain_event.event_id)

        if final_fragment:
            record_cycle_weather(cfg, cycle_id, final_fragment)

        try:
            TemporalCanonRepository(cfg).append(
                cycle_id, final_evaluation.verdict, final_evaluation.final_score,
                source_event_id=emitter.last_event_id,
            )
        except Exception as exc:
            # Best-effort, matching cycle.py's own temporal-state recording.
            logger.warning("Temporal canon state recording failed for cycle %d: %s", cycle_id, exc)

    emitter.emit(
        EVT_CYCLE_COMPLETED,
        aggregate_id=cycle_id,
        payload={"verdict": final_evaluation.verdict, "final_score": final_evaluation.final_score},
    )

    try:
        record_operation_manifest(
            cfg,
            run_id=emitter.correlation_id,
            operation="cycle",
            model=synthesizer_model or model or cfg.default_model,
            input_fingerprint=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            selected_evidence_ids=(
                [f"book:{row['canonical_ref']}" for row in book_rows]
                + [f"json:{row['id']}" for row in json_rows]
                + [f"canon:{row['ref']}" for row in canon_rows]
                + [f"agent:{out.name}" for out in agent_outputs]
            ),
            output_ids=[cycle_id],
        )
    except Exception as exc:
        # Best-effort, matching cycle.py's own manifest recording: auxiliary
        # provenance must not retroactively fail an already-completed cycle.
        logger.warning("Manifest recording failed for cycle %d: %s", cycle_id, exc)

    return cycle_id
