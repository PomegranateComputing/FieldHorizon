"""
Optional OCR: detection, gating, and the state a scan rests in.

Phase I refused scanned PDFs outright and called it a normalization
failure. That was right to refuse and wrong to call it a failure. A page
image is not a broken document -- it is a document whose text has not
been extracted yet, and the distinction matters because a failure is
something you fix in the code while a missing capability is something
you install.

So a scan now rests in **OCR_PENDING**, which is not an error state. If
an OCR backend appears later, those documents become processable without
re-downloading anything.

**Nothing here runs OCR by default, and nothing here runs OCR in bulk.**
Three separate gates must all open:

  1. a backend must actually be present (`fieldhorizon[ocr]`, or an
     external binary on PATH);
  2. `ocr.enabled` must be true in the policy;
  3. the per-run document ceiling must not be exhausted.

The ceiling exists because OCR is the one operation in this harvester
whose cost is measured in CPU-hours rather than in bytes, and because
running it across a corpus is a decision an operator should make
deliberately rather than discover afterwards.

**Institution-provided text always wins.** Gallica's text mode, OAPEN's
extracted TEXT bundle, and Wikisource's wikitext are all better than
anything this could produce locally: they are the provider's own
extraction, usually proofread. OCR is the last resort, not a shortcut
around implementing a provider's text interface.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Python packages that would provide in-process OCR. Neither is a
#: dependency of this project; both are looked for and neither is
#: required. `fieldhorizon[ocr]` is the extra that installs one.
_OPTIONAL_MODULES = ("pytesseract", "ocrmypdf")

#: External binaries that can do the job without a Python binding. An
#: operator who has tesseract installed should not also have to install a
#: wrapper library for `corpus health` to notice.
_EXTERNAL_BINARIES = ("ocrmypdf", "tesseract")

#: Environment variable naming an external OCR worker (a queue endpoint,
#: a container, a script). Detection only -- this module never invokes it.
OCR_WORKER_ENV = "FIELDHORIZON_OCR_WORKER"

#: Default per-run ceiling. Deliberately tiny: the brief for this phase
#: says explicitly not to run bulk OCR, and a default of 5 makes an
#: accidental corpus-wide OCR run impossible rather than merely unlikely.
DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN = 5


@dataclass(frozen=True)
class OcrBackend:
    """What is available to do OCR with, if anything."""

    kind: str = ""          # "python", "binary", "worker", or "" for none
    name: str = ""
    detail: str = ""

    @property
    def available(self) -> bool:
        return bool(self.kind)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "detail": self.detail,
                "available": self.available}


def detect_backend() -> OcrBackend:
    """
    Look for an OCR backend without importing anything heavyweight.

    `importlib.util.find_spec` rather than `import`: finding out whether
    OCR is available must not pull a large native library into every
    process that happens to ask, including `corpus health`.
    """
    import importlib.util

    worker = os.environ.get(OCR_WORKER_ENV, "").strip()
    if worker:
        # Not validated here on purpose. Reaching out to a queue endpoint
        # to see whether it answers is a side effect, and `corpus health`
        # should be safe to run at any time.
        return OcrBackend(kind="worker", name=OCR_WORKER_ENV, detail=worker)

    for module in _OPTIONAL_MODULES:
        try:
            if importlib.util.find_spec(module) is not None:
                return OcrBackend(kind="python", name=module, detail=f"{module} is importable")
        except (ImportError, ValueError):
            continue

    for binary in _EXTERNAL_BINARIES:
        path = shutil.which(binary)
        if path:
            return OcrBackend(kind="binary", name=binary, detail=path)

    return OcrBackend()


@dataclass(frozen=True)
class OcrDecision:
    """Whether a given document may be OCR'd right now, and why not."""

    allowed: bool
    reason: str = ""
    backend: OcrBackend = OcrBackend()

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "backend": self.backend.to_dict()}


def decide(
    *,
    policy_enabled: bool,
    documents_done: int = 0,
    max_documents: int = DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN,
    backend: OcrBackend | None = None,
) -> OcrDecision:
    """
    All three gates, evaluated in the order an operator would ask them.

    The reasons are worded to say what to DO. "no OCR backend installed"
    with the extra named beats "OCR unavailable", because the second
    tells an operator only that something is wrong.
    """
    resolved = backend if backend is not None else detect_backend()

    if not resolved.available:
        return OcrDecision(
            False,
            "no OCR backend installed: `pip install 'fieldhorizon[ocr]'`, put tesseract "
            f"or ocrmypdf on PATH, or set ${OCR_WORKER_ENV}",
            resolved,
        )
    if not policy_enabled:
        return OcrDecision(
            False,
            f"an OCR backend is available ({resolved.name}) but ocr.enabled is false "
            "in config/corpus_policy.yaml",
            resolved,
        )
    if max_documents <= 0:
        return OcrDecision(False, "the per-run OCR document ceiling is zero", resolved)
    if documents_done >= max_documents:
        return OcrDecision(
            False,
            f"the per-run OCR ceiling of {max_documents} document(s) is reached; "
            "the rest stay in OCR_PENDING for a later run",
            resolved,
        )
    return OcrDecision(True, "", resolved)


#: The marker `normalization` raises with when a PDF turns out to be page
#: images. Matched by the orchestrator to route the document to
#: OCR_PENDING instead of a failure state -- a string constant rather
#: than a substring search scattered across two modules.
NEEDS_OCR_MARKER = "needs-ocr"


class NeedsOCR(Exception):
    """
    A document's bytes are page images; text extraction needs OCR.

    Deliberately NOT a subclass of NormalizationError. The orchestrator
    treats a NormalizationError as final -- correctly, since a malformed
    EPUB will still be malformed next week -- and a scan is the opposite
    case: nothing is wrong with it, and a capability that does not exist
    today may exist tomorrow.
    """

    def __init__(self, detail: str = "", *, pages: int = 0) -> None:
        super().__init__(detail or "the document is page images with no text layer")
        self.detail = detail
        self.pages = pages
        self.marker = NEEDS_OCR_MARKER


def health_report(policy_enabled: bool, max_documents: int) -> dict:
    """What `corpus health` prints about OCR."""
    backend = detect_backend()
    decision = decide(
        policy_enabled=policy_enabled, documents_done=0,
        max_documents=max_documents, backend=backend,
    )
    return {
        "backend": backend.to_dict(),
        "policy_enabled": policy_enabled,
        "max_documents_per_run": max_documents,
        "would_run": decision.allowed,
        "reason": decision.reason,
        "note": (
            "Provider-supplied text is always preferred over local OCR: Gallica's text "
            "mode, OAPEN's extracted TEXT bundle, and Wikisource's wikitext are the "
            "provider's own extraction and usually proofread."
        ),
    }


__all__ = [
    "DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN",
    "NEEDS_OCR_MARKER",
    "OCR_WORKER_ENV",
    "NeedsOCR",
    "OcrBackend",
    "OcrDecision",
    "decide",
    "detect_backend",
    "health_report",
]
