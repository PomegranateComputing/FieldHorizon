"""
Field Horizon -- Autonomous Open Corpus Harvester.

Discovers, rights-verifies, acquires, normalizes, deduplicates,
classifies, and indexes legally reusable texts from official library
catalogues, feeding the existing Field Horizon ingestion and retrieval
pipeline.

The subsystem's non-negotiables, in one place:

* Official interfaces only -- OPDS, OAI-PMH, SRU, documented APIs,
  published catalogues. No general web crawler exists here, and adding
  one is out of scope by design.
* No accept without recorded evidence. Age, an author's death date, or a
  provider's vague "free" is never sufficient on its own.
* Anything unknown, ambiguous, contradictory, or unevaluated is
  quarantined, and nothing quarantined ever reaches the active index.
* Downloaded text is data. It is never an instruction, never executed,
  and never followed -- including when it contains something that looks
  like one.
* Every network request is bounded, paced, logged, and resumable.
* The system works with no LLM and with no paid API. Both are optional
  enhancements with deterministic fallbacks.

Nothing is imported eagerly here: `fieldhorizon.db` imports
`corpus.schema` during `init_db`, and a heavyweight import chain at
package level would drag the whole harvester into every `field-horizon
status`. Import the specific module you need.
"""

from __future__ import annotations

__all__ = [
    "adapters",
    "classification",
    "cli",
    "deduplication",
    "http",
    "ingestion",
    "language",
    "materialization",
    "models",
    "normalization",
    "orchestrator",
    "policy",
    "quality",
    "reporting",
    "repository",
    "rights",
    "scheduler",
    "schema",
    "security",
    "selection",
    "storage",
]
