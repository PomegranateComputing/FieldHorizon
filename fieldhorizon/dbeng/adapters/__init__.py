"""Engine adapters. Each implements adapters.base.DatabaseAdapter; nothing above this layer speaks SQL."""
from __future__ import annotations

from ..targets import TargetConfig
from .base import DatabaseAdapter


def open_adapter(target: TargetConfig) -> DatabaseAdapter:
    if target.engine == "postgresql":
        from .postgres import PostgresAdapter

        return PostgresAdapter(target)
    raise ValueError(f"No adapter for engine {target.engine!r}")
