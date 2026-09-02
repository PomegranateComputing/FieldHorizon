"""
Test-wide isolation.

Pilot defect #8 was the offline test suite writing its fixture catalogue
into the real `data/corpus/manifests/`, which a later live run read back
as though it were a provider's own catalogue -- and labelled a real
document with a fixture's title.

Phase I fixed the one path that caused it. This fixture addresses the
class: for the whole session, any write under the production data root
raises `ProductionWriteBlocked`, and opening the production database
raises at all. A test that would repeat defect #8 now fails at the moment
of the write instead of corrupting real data and surfacing later as a
mislabelled document.

Reads are deliberately left alone -- tests legitimately read the
repository's own `config.yaml`, `ontology.yaml`, and fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATA = REPO_ROOT / "data"
PRODUCTION_DATABASE = PRODUCTION_DATA / "field_horizon.sqlite3"


@pytest.fixture(scope="session", autouse=True)
def _block_production_data_writes():
    """
    Session-wide, automatic, and not opt-in.

    Made autouse because the failure it guards against came from library
    code several frames below a test that looked entirely innocent. A
    guard a test has to remember to request is not a guard.
    """
    from fieldhorizon.corpus.roots import install_test_guard, remove_test_guard

    install_test_guard(PRODUCTION_DATA, blocked_database=PRODUCTION_DATABASE)
    try:
        yield
    finally:
        remove_test_guard()
