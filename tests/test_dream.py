from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.dream import (
    DreamRegion,
    dream_run_events,
    list_dream_runs,
    run_dream,
    scan_for_ontology_proposals,
    write_ontology_proposal,
)


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root=tmp_path,
        database=tmp_path / "data.sqlite3",
        books=tmp_path / "books",
        json_corpus=tmp_path / "json_corpus",
        outputs=tmp_path / "outputs",
        logs=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b",
        temperature=1.25,
        top_p=0.95,
        repeat_penalty=1.08,
        num_ctx=8192,
        book_fragments=6,
        json_entries=8,
        chunk_chars=1800,
        chunk_overlap=250,
        tone="dark",
        mode="canonical_synthesis",
        manifestos=tmp_path / "manifestos",
        embedding_model="nomic-embed-text",
    )


_FAKE_REGION = DreamRegion(kind="domain", label="tawhid", query="tawhid", detail={"domain": "tawhid"})


def test_run_dream_respects_budget_cycles(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=3, budget_minutes=60.0)

    assert run.iterations == 3


def test_run_dream_respects_budget_minutes_of_zero(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=1000, budget_minutes=0.0)

    assert run.iterations == 0


def test_run_dream_stops_when_no_region_is_available(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=None),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=5, budget_minutes=60.0)

    assert run.iterations == 1
    assert run.events[-1]["event"] == "no_region_available"


def test_run_dream_writes_ledger_and_summary_outside_data(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    assert run.ledger_path.exists()
    assert run.summary_path.exists()
    # Never-touch list: neither output lands under books/, manifestos/, or json_corpus/.
    for forbidden_dir in (cfg.books, cfg.manifestos, cfg.json_corpus):
        assert not str(run.ledger_path).startswith(str(forbidden_dir))
        assert not str(run.summary_path).startswith(str(forbidden_dir))


def test_list_dream_runs_is_empty_with_no_runs(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert list_dream_runs(cfg) == []


def test_list_dream_runs_reads_back_a_real_run(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=2, budget_minutes=60.0)

    runs = list_dream_runs(cfg)
    assert len(runs) == 1
    summary = runs[0]
    assert summary.iterations == 2
    assert summary.budget_cycles == 2
    assert summary.budget_minutes == 60.0
    assert summary.regions_explored == 2
    assert summary.proposals_written == 0
    assert summary.started_at == run.started_at
    assert summary.finished_at == run.finished_at


def test_list_dream_runs_orders_newest_first(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    # run_dream's ledger/summary filenames key off a second-precision
    # timestamp; two runs within the same wall-clock second would collide
    # and overwrite each other's files (a real, out-of-scope-here
    # limitation of that stamp), so this test's own two calls need to
    # land in different seconds to prove ordering at all.
    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)
        time.sleep(1.1)
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    runs = list_dream_runs(cfg)
    assert len(runs) == 2
    assert runs[0].stamp >= runs[1].stamp


def test_list_dream_runs_skips_a_ledger_with_no_summary_file(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    dreams_dir = cfg.logs / "dreams"
    dreams_dir.mkdir(parents=True)
    (dreams_dir / "dream_2020-01-01_00-00-00.jsonl").write_text('{"event": "no_region_available"}\n', encoding="utf-8")

    assert list_dream_runs(cfg) == []


def test_dream_run_events_returns_none_for_an_unknown_stamp(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert dream_run_events(cfg, "2020-01-01_00-00-00") is None


def test_dream_run_events_rejects_a_malformed_stamp(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert dream_run_events(cfg, "../../etc/passwd") is None


def test_dream_run_events_reads_back_the_real_ledger(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    stamp = list_dream_runs(cfg)[0].stamp
    events = dream_run_events(cfg, stamp)
    assert events is not None
    assert events[0]["event"] == "region_iteration"


def test_run_dream_never_writes_to_json_corpus_directory(tmp_path):
    """
    Explicit safety-rail check: even a promoted axiom mutation must not
    place a new file under cfg.json_corpus (which -- like ontology.yaml
    and sources.yaml -- is content the user curates, not dream's to grow
    unsupervised). Mutation candidates land under outputs/dreams/ instead.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)

    opposition_region = DreamRegion(
        kind="opposition", label="tawhid vs multiplicity", query="tawhid versus multiplicity",
        detail={"category_a": "tawhid", "category_b": "multiplicity", "reason": "unity vs fragmentation"},
    )

    with (
        patch("fieldhorizon.dream.choose_region", return_value=opposition_region),
        patch("fieldhorizon.dream._matching_opposition_edge", return_value=None),
        patch("fieldhorizon.dream.run_cycle", return_value=1),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    assert list(cfg.json_corpus.glob("**/*")) == []


def test_scan_for_ontology_proposals_finds_low_confidence_recurring_motifs(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    from fieldhorizon.db import connect

    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.commit()
        source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]

        chunk_ids = []
        for i in range(6):
            cur = conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
                "VALUES (?, ?, ?, 'content', 1)",
                (source_id, i, f"ref{i}"),
            )
            chunk_ids.append(cur.lastrowid)

        for cid in chunk_ids:
            conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (?, 'the veiled machine')", (cid,))
            # Low confidence domain tag -- below PROPOSAL_CONFIDENCE_THRESHOLD.
            conn.execute("INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (?, 'technology', 0.2)", (cid,))
        conn.commit()

    evidence = scan_for_ontology_proposals(cfg)
    assert len(evidence) == 1
    assert evidence[0]["motif"] == "the veiled machine"
    assert evidence[0]["count"] == 6


def test_scan_for_ontology_proposals_ignores_confidently_tagged_motifs(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    from fieldhorizon.db import connect

    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.commit()
        source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]

        chunk_ids = []
        for i in range(6):
            cur = conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
                "VALUES (?, ?, ?, 'content', 1)",
                (source_id, i, f"ref{i}"),
            )
            chunk_ids.append(cur.lastrowid)

        for cid in chunk_ids:
            conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (?, 'well understood motif')", (cid,))
            # High confidence -- the ontology already accounts for this.
            conn.execute("INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (?, 'technology', 0.95)", (cid,))
        conn.commit()

    assert scan_for_ontology_proposals(cfg) == []


def test_write_ontology_proposal_has_the_required_structure(tmp_path):
    cfg = make_config(tmp_path)
    evidence = {
        "motif": "the veiled machine",
        "count": 7,
        "sample_chunks": [{"id": 1, "excerpt": "some excerpt"}],
    }

    path = write_ontology_proposal(cfg, evidence)
    assert path.exists()
    assert path.parent == cfg.root / "proposals" / "ontology"

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["domain"]["name"] == "the_veiled_machine"
    assert data["domain"]["hints"] == ["the veiled machine"]
    assert "keywords" in data["domain"]
    assert "rewrite_directive" in data["domain"]
    assert data["evidence"]["motif"] == "the veiled machine"
    assert data["evidence"]["count"] == 7
    assert data["evidence"]["sample_chunks"] == evidence["sample_chunks"]
    assert isinstance(data["rationale"], str) and len(data["rationale"]) > 0


def test_write_ontology_proposal_numbers_sequentially(tmp_path):
    cfg = make_config(tmp_path)
    path1 = write_ontology_proposal(cfg, {"motif": "first motif", "count": 5, "sample_chunks": []})
    path2 = write_ontology_proposal(cfg, {"motif": "second motif", "count": 5, "sample_chunks": []})
    assert path1.name.startswith("0001_")
    assert path2.name.startswith("0002_")


def test_run_dream_writes_a_proposal_file_when_evidence_exists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    fake_evidence = [{"motif": "the veiled machine", "count": 6, "sample_chunks": []}]

    with (
        patch("fieldhorizon.dream.choose_region", return_value=None),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=fake_evidence),
    ):
        run = run_dream(cfg, budget_cycles=5, budget_minutes=60.0)

    assert len(run.proposals_written) == 1
    proposal_path = Path(run.proposals_written[0])
    assert proposal_path.exists()
    assert "veiled_machine" in proposal_path.name


def test_run_dream_summary_is_readable_markdown(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "CANON"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    text = run.summary_path.read_text(encoding="utf-8")
    assert text.startswith("# Field Horizon Dream")
    assert "domain" in text
