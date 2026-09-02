from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import AppConfig
from .events import (
    ACTOR_CLI,
    AGGREGATE_PROPOSAL,
    EVT_PROPOSAL_APPLIED,
    EVT_PROPOSAL_APPLY_FAILED,
    EVT_PROPOSAL_REJECTED,
    OperationEmitter,
)
from .ontology_spec import DEFAULT_ONTOLOGY_PATH, clear_ontology_cache


class ProposalError(ValueError):
    """A proposals CLI operation failed -- proposal missing, malformed, or the domain already exists."""


def _proposals_dir(cfg: AppConfig) -> Path:
    return cfg.root / "proposals" / "ontology"


def _rejected_dir(cfg: AppConfig) -> Path:
    return cfg.root / "proposals" / "rejected"


def _applied_dir(cfg: AppConfig) -> Path:
    return cfg.root / "proposals" / "applied"


def list_proposal_paths(cfg: AppConfig) -> list[Path]:
    proposals_dir = _proposals_dir(cfg)
    if not proposals_dir.exists():
        return []
    return sorted(p for p in proposals_dir.glob("*.yaml") if p.name[:4].isdigit())


def _find_proposal(cfg: AppConfig, number: int) -> Path:
    for path in list_proposal_paths(cfg):
        if int(path.name[:4]) == number:
            return path
    raise ProposalError(f"No pending proposal numbered {number:04d}")


@dataclass(frozen=True)
class ProposalSummary:
    number: int
    name: str
    path: Path
    motif: str
    count: int


def list_proposals(cfg: AppConfig) -> list[ProposalSummary]:
    summaries = []
    for path in list_proposal_paths(cfg):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        summaries.append(
            ProposalSummary(
                number=int(path.name[:4]),
                name=data["domain"]["name"],
                path=path,
                motif=data["evidence"]["motif"],
                count=data["evidence"]["count"],
            )
        )
    return summaries


def show_proposal(cfg: AppConfig, number: int) -> dict:
    path = _find_proposal(cfg, number)
    data: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def reject_proposal(cfg: AppConfig, number: int, actor: str = ACTOR_CLI) -> Path:
    path = _find_proposal(cfg, number)
    rejected_dir = _rejected_dir(cfg)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = rejected_dir / path.name
    path.rename(dest)

    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_PROPOSAL)
    emitter.emit(EVT_PROPOSAL_REJECTED, aggregate_id=number, payload={"path": str(dest)})

    return dest


def _run_ontology_parity_check(cfg: AppConfig) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_rust_parity.py", "-q"],
        cwd=cfg.root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProposalError(
            "Ontology parity test failed after merging the proposal -- reverted, not committed.\n"
            f"{result.stdout}\n{result.stderr}"
        )


def apply_proposal(cfg: AppConfig, number: int, actor: str = ACTOR_CLI) -> Path:
    """
    Merges the proposal's domain block into ontology.yaml, re-runs the
    Python/Rust ontology parity test, and only if it passes, git-commits
    the change with the proposal id in the message. On parity failure the
    file is reverted to its original content before raising -- ontology.yaml
    is never left in a half-merged, uncommitted state either way.

    Emits ProposalApplied on success or ProposalApplyFailed on either
    failure path -- FABLE Sec.14 requires this app's destructive actions
    (this is its most consequential one: a merge plus a real git commit)
    to be event-logged, not just confirmed in the UI.
    """
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_PROPOSAL)
    path = _find_proposal(cfg, number)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    domain_block = dict(data["domain"])
    name = domain_block.pop("name")

    original_text = DEFAULT_ONTOLOGY_PATH.read_text(encoding="utf-8")
    raw = yaml.safe_load(original_text)

    if name in (raw.get("domains") or {}):
        emitter.emit(EVT_PROPOSAL_APPLY_FAILED, aggregate_id=number, payload={"reason": "domain_already_exists", "name": name})
        raise ProposalError(f"domain {name!r} already exists in ontology.yaml -- cannot apply")

    raw.setdefault("domains", {})[name] = domain_block

    DEFAULT_ONTOLOGY_PATH.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    clear_ontology_cache()

    try:
        _run_ontology_parity_check(cfg)
    except ProposalError as exc:
        DEFAULT_ONTOLOGY_PATH.write_text(original_text, encoding="utf-8")
        clear_ontology_cache()
        emitter.emit(EVT_PROPOSAL_APPLY_FAILED, aggregate_id=number, payload={"reason": "parity_check_failed", "name": name, "detail": str(exc)})
        raise

    applied_dir = _applied_dir(cfg)
    applied_dir.mkdir(parents=True, exist_ok=True)
    dest = applied_dir / path.name
    path.rename(dest)

    subprocess.run(
        ["git", "add", str(DEFAULT_ONTOLOGY_PATH), str(dest)], cwd=cfg.root, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "commit", "-m",
            f"Apply ontology proposal {number:04d}: add domain '{name}'\n\n"
            f"Evidence: motif {data['evidence']['motif']!r} across {data['evidence']['count']} chunks.",
        ],
        cwd=cfg.root,
        check=True,
        capture_output=True,
    )

    emitter.emit(EVT_PROPOSAL_APPLIED, aggregate_id=number, payload={"name": name, "path": str(dest)})

    return dest
