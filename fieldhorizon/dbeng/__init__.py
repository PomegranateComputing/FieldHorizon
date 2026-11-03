"""
Field Horizon DB engineering: observe, evaluate, change and validate a
database -- autonomously, inside an explicitly configured authority.

The package is deliberately separate from the doctrine engine. It reuses
that engine's infrastructure (the local Ollama client, domain events,
provenance edges, the model registry) and writes its own state into
additive ``dbeng_*`` tables of the Field Horizon database. It never stores
a target database's business data there, and it never alters a
pre-existing Field Horizon table.

Layers, from the bottom:

* ``targets``   -- which databases exist, how to reach them; no secrets.
* ``adapters``  -- engine-specific I/O behind an engine-neutral protocol
                   (PostgreSQL today; the protocol is what a SQL Server
                   adapter would implement).
* ``discovery`` / ``advisor`` / ``workload`` -- read-only facts:
                   snapshots, query plans, measurements, index candidates.
* ``policy``    -- the deterministic authority. Nothing an LLM says can
                   grant or deny a change, or declare one successful.
* ``agents``    -- LLM roles whose every claim must cite evidence ids;
                   uncited claims are discarded, not weighed.
* ``operation`` -- the journaled, recoverable state machine that runs an
                   authorized operation end to end without a human.
"""
