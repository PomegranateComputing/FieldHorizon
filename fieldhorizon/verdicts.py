from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .evaluate import CycleEvaluation

VERDICT_DIRS = {
    "CANON": "canon",
    "USEFUL_FRAGMENT": "useful",
    "HERESY": "heresy",
    "NOISE": "noise",
}


def canon_dir_for_verdict(cfg: AppConfig, verdict: str) -> Path:
    return cfg.outputs / VERDICT_DIRS.get(verdict, "noise")


def should_rewrite(evaluation: CycleEvaluation) -> bool:
    return (
        evaluation.verdict in {"HERESY", "NOISE"}
        or evaluation.doctrinal_enforcement < 0.7
        or evaluation.final_score < 0.75
    )
