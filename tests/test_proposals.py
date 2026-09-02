from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.dream import write_ontology_proposal
from fieldhorizon.events import EventRepository
from fieldhorizon.proposals import (
    ProposalError,
    apply_proposal,
    list_proposals,
    reject_proposal,
    show_proposal,
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


def _sample_evidence(motif: str = "the veiled machine") -> dict:
    return {"motif": motif, "count": 6, "sample_chunks": [{"id": 1, "excerpt": "an excerpt"}]}


def _fake_ontology_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "ontology.yaml"
    path.write_text(
        "domains:\n  tawhid:\n    hints: [tawhid]\n    expansions: []\n    sources: []\n"
        "    keywords: []\n    rewrite_directive: null\n"
        "oppositions: []\nsource_expansions: {}\n",
        encoding="utf-8",
    )
    return path


def test_list_proposals_empty_when_none_written(tmp_path):
    cfg = make_config(tmp_path)
    assert list_proposals(cfg) == []


def test_list_proposals_reports_written_proposals(tmp_path):
    cfg = make_config(tmp_path)
    write_ontology_proposal(cfg, _sample_evidence())

    summaries = list_proposals(cfg)
    assert len(summaries) == 1
    assert summaries[0].number == 1
    assert summaries[0].motif == "the veiled machine"


def test_show_proposal_returns_full_structure(tmp_path):
    cfg = make_config(tmp_path)
    write_ontology_proposal(cfg, _sample_evidence())

    data = show_proposal(cfg, 1)
    assert data["domain"]["name"] == "the_veiled_machine"
    assert data["evidence"]["motif"] == "the veiled machine"


def test_show_proposal_raises_for_unknown_number(tmp_path):
    cfg = make_config(tmp_path)
    with pytest.raises(ProposalError, match="No pending proposal"):
        show_proposal(cfg, 999)


def test_reject_proposal_moves_file_to_rejected_dir(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    original = write_ontology_proposal(cfg, _sample_evidence())

    dest = reject_proposal(cfg, 1)

    assert not original.exists()
    assert dest.exists()
    assert dest.parent == cfg.root / "proposals" / "rejected"
    assert list_proposals(cfg) == []


def test_reject_proposal_emits_a_domain_event(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_ontology_proposal(cfg, _sample_evidence())

    reject_proposal(cfg, 1)

    events = EventRepository(cfg).by_aggregate("proposal", 1)
    assert [e.event_type for e in events] == ["ProposalRejected"]


def test_apply_proposal_merges_domain_and_commits_on_parity_success(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    original = write_ontology_proposal(cfg, _sample_evidence())
    fake_ontology = _fake_ontology_yaml(tmp_path)

    with (
        patch("fieldhorizon.proposals.DEFAULT_ONTOLOGY_PATH", fake_ontology),
        patch("fieldhorizon.proposals._run_ontology_parity_check"),
        patch("fieldhorizon.proposals.subprocess.run") as mock_run,
    ):
        dest = apply_proposal(cfg, 1)

    merged = yaml.safe_load(fake_ontology.read_text(encoding="utf-8"))
    assert "the_veiled_machine" in merged["domains"]
    assert merged["domains"]["the_veiled_machine"]["hints"] == ["the veiled machine"]

    assert not original.exists()
    assert dest.exists()
    assert dest.parent == cfg.root / "proposals" / "applied"

    commit_call = mock_run.call_args_list[-1]
    assert "commit" in commit_call.args[0]
    assert any("0001" in arg for arg in commit_call.args[0])

    events = EventRepository(cfg).by_aggregate("proposal", 1)
    assert [e.event_type for e in events] == ["ProposalApplied"]


def test_apply_proposal_reverts_ontology_yaml_when_parity_check_fails(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_ontology_proposal(cfg, _sample_evidence())
    fake_ontology = _fake_ontology_yaml(tmp_path)
    original_text = fake_ontology.read_text(encoding="utf-8")

    with (
        patch("fieldhorizon.proposals.DEFAULT_ONTOLOGY_PATH", fake_ontology),
        patch("fieldhorizon.proposals._run_ontology_parity_check", side_effect=ProposalError("parity failed")),
        patch("fieldhorizon.proposals.subprocess.run") as mock_run,
        pytest.raises(ProposalError, match="parity failed"),
    ):
        apply_proposal(cfg, 1)

    # ontology.yaml is back to exactly what it was -- no half-merged state.
    assert fake_ontology.read_text(encoding="utf-8") == original_text
    # No git commit happened.
    mock_run.assert_not_called()
    # The proposal file is still pending -- not moved to applied/.
    assert list_proposals(cfg)

    events = EventRepository(cfg).by_aggregate("proposal", 1)
    assert [e.event_type for e in events] == ["ProposalApplyFailed"]


def test_apply_proposal_rejects_a_domain_name_that_already_exists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_ontology_proposal(cfg, _sample_evidence(motif="tawhid"))
    fake_ontology = _fake_ontology_yaml(tmp_path)  # already declares a 'tawhid' domain

    with (
        patch("fieldhorizon.proposals.DEFAULT_ONTOLOGY_PATH", fake_ontology),
        pytest.raises(ProposalError, match="already exists"),
    ):
        apply_proposal(cfg, 1)

    events = EventRepository(cfg).by_aggregate("proposal", 1)
    assert [e.event_type for e in events] == ["ProposalApplyFailed"]
