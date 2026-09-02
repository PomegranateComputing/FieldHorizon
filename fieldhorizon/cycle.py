from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from .canon import load_canon_fragments, load_motif_counts, record_canon_ngrams
from .config import AppConfig
from .critic import critique_response
from .db import connect
from .embeddings import embed_and_store_fragment
from .evaluate import evaluate_cycle, evaluation_to_markdown
from .events import (
    ACTOR_CLI,
    AGGREGATE_CYCLE,
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
from .manifests import record_operation_manifest
from .principal import PrincipalContext
from .prompting import build_prompt
from .provenance import record_selected_by_edges, record_supports_edges
from .retrieval import search_books, search_json
from .rewrite import rewrite_response
from .synthesis import build_structured_response, generate_fragment
from .temporal import TemporalCanonRepository
from .verdicts import canon_dir_for_verdict, should_rewrite
from .weather import record_cycle_weather

logger = logging.getLogger(__name__)


def row_to_dict(row) -> dict:
    # sqlite3.Row iterates its *values*, not its column names -- .keys()
    # is required here (ruff's SIM118 doesn't know this isn't a plain dict).
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


def run_cycle(
    cfg: AppConfig,
    query: str,
    model: str | None = None,
    dry_run: bool = False,
    auto_rewrite: bool = False,
    critic_model: str | None = None,
    rewrite_model: str | None = None,
    json_domain: str | None = None,
    actor: str = ACTOR_CLI,
    principal: PrincipalContext | None = None,
) -> int:
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_CYCLE, principal=principal)

    try:
        return _run_cycle_body(
            cfg, query, model, dry_run, auto_rewrite, critic_model, rewrite_model, json_domain, emitter
        )
    except Exception as exc:
        emitter.emit(EVT_CYCLE_FAILED, payload={"error_type": type(exc).__name__, "error_message": str(exc)})
        raise


def _run_cycle_body(
    cfg: AppConfig,
    query: str,
    model: str | None,
    dry_run: bool,
    auto_rewrite: bool,
    critic_model: str | None,
    rewrite_model: str | None,
    json_domain: str | None,
    emitter: OperationEmitter,
) -> int:
    emitter.emit(EVT_CYCLE_STARTED, payload={"query": query, "model": model or cfg.default_model, "dry_run": dry_run})

    book_rows = search_books(cfg, query)
    json_rows = search_json(cfg, query, domain=json_domain)
    canon_rows = load_canon_fragments(cfg, query=query, limit=3)
    motif_counts = load_motif_counts(cfg)
    parent_cycle_ids = parent_cycle_ids_for(canon_rows)

    emitter.emit(
        EVT_RETRIEVAL_COMPLETED,
        payload={"book_count": len(book_rows), "json_count": len(json_rows), "canon_count": len(canon_rows)},
    )

    prompt = build_prompt(cfg, query, book_rows, json_rows, canon_rows)

    source_refs = [row["canonical_ref"] for row in book_rows]
    json_refs = [row["id"] for row in json_rows]
    canon_refs = [row["ref"] for row in canon_rows]
    json_dicts = [row_to_dict(row) for row in json_rows]

    fragment_text = "[DRY RUN]\n\n" + prompt if dry_run else generate_fragment(cfg, prompt, model=model)

    response = build_structured_response(
        fragment_text=fragment_text,
        query=query,
        source_refs=source_refs,
        json_refs=json_refs,
        canon_refs=canon_refs,
        dominant_traditions=["single_cycle", "recursive_canon"],
        tags=["single_cycle", "synthesis"],
    )

    evaluation = evaluate_cycle(response, json_dicts, motif_counts=motif_counts, cfg=cfg)
    evaluation_md = evaluation_to_markdown(evaluation)

    critique = ""
    rewritten_response = ""
    rewritten_evaluation = None
    rewritten_evaluation_md = ""

    final_response = response
    final_evaluation = evaluation
    final_evaluation_md = evaluation_md

    if auto_rewrite and not dry_run and should_rewrite(evaluation):
        critique = critique_response(
            cfg=cfg,
            original_prompt=prompt,
            response=response,
            model=critic_model or model,
        )

        raw_rewritten_response = rewrite_response(
            cfg=cfg,
            original_prompt=prompt,
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
            dominant_traditions=["single_cycle", "recursive_canon"],
            tags=["single_cycle", "synthesis"],
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
    log_path = cfg.logs / f"cycle_{stamp}.md"

    full_log = (
        f"# Field Horizon Cycle {stamp}\n\n"
        f"## Query\n{query}\n\n"
        f"## Prompt\n```text\n{prompt}\n```\n\n"
        f"## Initial Response\n{response}\n\n"
        f"{evaluation_md}\n"
    )

    if critique:
        full_log += (
            f"\n## Critique\n{critique}\n\n"
            f"## Rewritten Response\n{rewritten_response}\n\n"
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
                prompt,
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
        # Best-effort, like record_operation_manifest below: a provenance
        # edge write failure must not retroactively turn an already-
        # completed cycle into a false CycleFailed event.
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
            # record_canon_event cross-emits FragmentPromoted itself (the
            # one place that cross-reference happens); adopt() keeps this
            # emitter's causation chain pointing at it, so CycleCompleted
            # below chains from FragmentPromoted rather than skipping it.
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
            # Best-effort, matching every other Phase C/D write hook: a
            # temporal-state write failure must not fail the cycle it describes.
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
            model=model or cfg.default_model,
            input_fingerprint=hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            selected_evidence_ids=(
                [f"book:{row['canonical_ref']}" for row in book_rows]
                + [f"json:{row['id']}" for row in json_rows]
                + [f"canon:{row['ref']}" for row in canon_rows]
            ),
            output_ids=[cycle_id],
        )
    except Exception as exc:
        # Best-effort, like embed_and_store_fragment/record_cycle_weather:
        # manifest recording is auxiliary provenance, not the cycle's own
        # result -- a failure here must not retroactively turn an already-
        # completed cycle into a false CycleFailed event.
        logger.warning("Manifest recording failed for cycle %d: %s", cycle_id, exc)

    return cycle_id