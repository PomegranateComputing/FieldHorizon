from __future__ import annotations

from pathlib import Path

from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.verdicts import canon_dir_for_verdict, should_rewrite


def make_config(tmp_path: Path):
    from fieldhorizon.config import AppConfig

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


def make_evaluation(verdict: str, final_score: float = 0.9, doctrinal_enforcement: float = 0.9) -> CycleEvaluation:
    return CycleEvaluation(
        symbolic_density=1.0,
        symbolic_density_method="keyword",
        doctrinal_enforcement=doctrinal_enforcement,
        doctrinal_enforcement_method="keyword",
        length_score=1.0,
        structure_score=1.0,
        stuffing_penalty=0.0,
        generic_penalty=0.0,
        final_score=final_score,
        verdict=verdict,
        notes=[],
    )


def test_canon_dir_for_verdict_maps_each_verdict(tmp_path):
    cfg = make_config(tmp_path)
    assert canon_dir_for_verdict(cfg, "CANON") == cfg.outputs / "canon"
    assert canon_dir_for_verdict(cfg, "USEFUL_FRAGMENT") == cfg.outputs / "useful"
    assert canon_dir_for_verdict(cfg, "HERESY") == cfg.outputs / "heresy"
    assert canon_dir_for_verdict(cfg, "NOISE") == cfg.outputs / "noise"
    assert canon_dir_for_verdict(cfg, "UNKNOWN") == cfg.outputs / "noise"


def test_should_rewrite_true_for_heresy_and_noise():
    assert should_rewrite(make_evaluation("HERESY"))
    assert should_rewrite(make_evaluation("NOISE"))


def test_should_rewrite_false_for_strong_canon():
    assert not should_rewrite(make_evaluation("CANON", final_score=0.9, doctrinal_enforcement=0.9))


def test_should_rewrite_true_for_weak_useful_fragment():
    assert should_rewrite(make_evaluation("USEFUL_FRAGMENT", final_score=0.7, doctrinal_enforcement=0.9))
