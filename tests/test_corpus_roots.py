"""
Data roots and test isolation.

Pilot defect #8 was the worst of the eleven: a cache path resolved
against the working directory, so the offline test suite wrote its
fixture catalogue into the real `data/corpus/manifests/`, and a live run
later read it back as though it were Project Gutenberg's own catalogue --
labelling a real French document with a fixture's title.

Phase I fixed that one path. These tests address the class, and the
first of them reproduces the original defect directly: it fails if a
data path is ever resolved against `os.getcwd()`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fieldhorizon.corpus.roots import (
    DATA_ROOT_ENV,
    DataRoots,
    PathEscapeError,
    ProductionWriteBlocked,
    guard_is_active,
    resolve,
)
from tests.conftest import PRODUCTION_DATA, PRODUCTION_DATABASE, REPO_ROOT
from tests.corpus_helpers import make_config

# --------------------------------------------------- reproducing defect #8


def test_a_relative_base_is_refused_rather_than_resolved_against_the_cwd(tmp_path, monkeypatch):
    """
    **The defect #8 reproduction.**

    The original bug was `Path("data/corpus/manifests")` -- a relative
    literal that Python resolves against `os.getcwd()`. Running the test
    suite from the repository root therefore wrote into real data.

    `resolve()` refuses a relative base outright. If this test ever
    starts passing a relative base successfully, defect #8 is back.
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PathEscapeError, match="current working directory"):
        resolve(Path("data/corpus/manifests"), "catalogue.csv")

    # The same call with an absolute base is fine.
    absolute = resolve(tmp_path / "data" / "corpus", "manifests", "catalogue.csv")
    assert absolute.is_absolute()
    assert tmp_path in absolute.parents


def test_the_same_relative_path_would_have_produced_different_targets(tmp_path, monkeypatch):
    """
    Why the refusal matters: the identical expression yields different
    destinations depending only on where the process was started. That
    is what made the bug invisible until a live run disagreed with
    itself.
    """
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    from_first = (Path("data") / "x.csv").resolve()
    monkeypatch.chdir(second)
    from_second = (Path("data") / "x.csv").resolve()

    assert from_first != from_second, "this is the bug: same code, two destinations"

    # A declared root gives the same answer from anywhere.
    root = tmp_path / "declared"
    assert resolve(root, "data", "x.csv") == resolve(root, "data", "x.csv")


def test_resolve_refuses_to_escape_its_root(tmp_path):
    with pytest.raises(PathEscapeError, match="escapes its root"):
        resolve(tmp_path / "corpus", "..", "..", "etc", "passwd")


# ------------------------------------------------------------- data roots


def test_every_root_is_absolute(tmp_path):
    roots = DataRoots.from_config(make_config(tmp_path))
    for name, path in roots.all_roots().items():
        assert path.is_absolute(), f"{name} is not absolute: {path}"


def test_the_corpus_subtree_hangs_off_the_data_root(tmp_path):
    roots = DataRoots.from_config(make_config(tmp_path))
    for path in (roots.blobs, roots.normalized, roots.licenses, roots.metadata,
                 roots.quarantine, roots.locks, roots.reports, roots.manifests, roots.dumps):
        assert roots.corpus in path.parents
        assert roots.contains(path)


def test_an_env_override_relocates_the_data_subtree(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(DATA_ROOT_ENV, str(elsewhere))

    roots = DataRoots.from_config(make_config(tmp_path))
    assert roots.data == elsewhere.resolve()
    assert roots.corpus == (elsewhere / "corpus").resolve()
    # The code root is unchanged: separating data from code is the point.
    assert roots.root == tmp_path.resolve()


def test_a_relative_env_override_is_refused(tmp_path, monkeypatch):
    """
    A relative `$FIELDHORIZON_DATA_ROOT` would reintroduce the defect
    through the very mechanism added to prevent it.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, "some/relative/path")

    with pytest.raises(PathEscapeError, match="must be an absolute path"):
        DataRoots.from_config(make_config(tmp_path))


def test_roots_are_logged_at_startup(tmp_path, caplog):
    """
    "Where is it writing" must never require reading code. A root that
    has silently moved should be visible in the first lines of a run log.
    """
    import logging

    roots = DataRoots.from_config(make_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="fieldhorizon.corpus.roots"):
        roots.log_resolved()

    logged = caplog.text
    assert "data roots" in logged
    for name in ("blobs", "normalized", "quarantine", "database", "logs"):
        assert name in logged
    assert str(tmp_path) in logged


def test_the_orchestrator_resolves_and_logs_its_roots(tmp_path, caplog):
    import logging

    from fieldhorizon.corpus.orchestrator import Orchestrator
    from fieldhorizon.db import init_db
    from tests.corpus_helpers import make_fetcher, make_policy, make_source

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with caplog.at_level(logging.INFO, logger="fieldhorizon.corpus.roots"):
        orchestrator = Orchestrator(
            cfg, make_policy(), [make_source()], fetcher=make_fetcher({}), emit_events=False
        )

    assert orchestrator.roots.corpus.is_absolute()
    assert tmp_path.resolve() in orchestrator.roots.corpus.parents
    assert "data roots" in caplog.text


def test_roots_serialize_for_the_run_report(tmp_path):
    payload = DataRoots.from_config(make_config(tmp_path)).to_dict()
    assert all(Path(p).is_absolute() for p in payload.values())
    assert set(payload) >= {"corpus", "blobs", "database", "logs", "outputs"}


# --------------------------------------------- the production write guard


def test_the_guard_is_active_for_the_whole_session():
    """
    Installed autouse in conftest. A guard a test has to remember to
    request is not a guard -- the failure it protects against came from
    library code several frames below a test that looked innocent.
    """
    assert guard_is_active()


def test_writing_into_the_production_data_root_raises():
    """
    The mechanism that makes defect #8 impossible from a test. This is
    the exact operation the Phase I suite performed by accident.
    """
    target = PRODUCTION_DATA / "corpus" / "manifests" / "catalogue_from_a_test.csv"

    with pytest.raises(ProductionWriteBlocked, match="production data root"), open(target, "w") as handle:
        handle.write("Text#,Type,Issued,Title\n")

    assert not target.exists()


def test_appending_to_production_data_also_raises():
    with pytest.raises(ProductionWriteBlocked), open(PRODUCTION_DATA / "appended_by_a_test.txt", "a") as handle:
        handle.write("x")


def test_opening_the_production_database_raises():
    """
    Blocked read or write. A test has no business opening it at all, and
    a read that succeeds today becomes a write when someone extends the
    test.
    """
    import sqlite3

    with pytest.raises(ProductionWriteBlocked, match="production database"):
        sqlite3.connect(str(PRODUCTION_DATABASE))


def test_reading_repository_files_is_still_allowed():
    """
    Reads are deliberately untouched: tests legitimately read the
    repository's own config, ontology, and fixtures.
    """
    assert (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    assert (REPO_ROOT / "config" / "corpus_sources.yaml").read_text(encoding="utf-8")


def test_writing_to_a_temporary_directory_is_unaffected(tmp_path):
    target = tmp_path / "corpus" / "blobs" / "x"
    target.parent.mkdir(parents=True)
    target.write_text("fine", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "fine"


def test_an_in_memory_database_is_unaffected():
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.close()


def test_the_whole_corpus_suite_writes_only_to_temporary_directories(tmp_path):
    """
    An end-to-end harvest under the guard. If any part of the pipeline
    still resolved a path against the working directory, this would
    raise -- which is precisely what would have happened in Phase I.
    """
    from fieldhorizon.corpus.orchestrator import Orchestrator
    from fieldhorizon.db import init_db
    from tests.corpus_helpers import make_fetcher, make_policy, make_source
    from tests.test_corpus_pipeline import FEED_URL, _feed_fixtures

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    orchestrator = Orchestrator(
        cfg,
        make_policy(),
        [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)],
        fetcher=make_fetcher(_feed_fixtures()),
        emit_events=False,
    )
    orchestrator.run_sync("broad")

    assert orchestrator.stats.ingested >= 1
    assert tmp_path.resolve() in orchestrator.roots.corpus.parents


def test_the_test_config_never_points_at_production(tmp_path):
    cfg = make_config(tmp_path)
    roots = DataRoots.from_config(cfg)

    for name, path in roots.all_roots().items():
        assert PRODUCTION_DATA not in path.parents, f"{name} points into production: {path}"
        assert path != PRODUCTION_DATABASE


def test_source_cache_dirs_are_absolute_and_temporary():
    """
    The specific Phase I fix, re-asserted: `make_source` must hand out an
    absolute temporary cache directory, never a relative default.
    """
    from tests.corpus_helpers import make_source

    source = make_source()
    assert source.cache_dir is not None
    assert source.cache_dir.is_absolute()
    assert PRODUCTION_DATA not in source.cache_dir.parents


def test_the_guard_does_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    """
    Blocking must be by resolved path, not by a relative comparison --
    otherwise `chdir` would defeat it, which is the same class of bug all
    over again.
    """
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ProductionWriteBlocked), open(PRODUCTION_DATA / "via_a_different_cwd.txt", "w") as handle:
        handle.write("x")


def test_a_relative_path_that_lands_in_production_is_still_blocked(monkeypatch):
    """
    `chdir` into the production data directory and write a bare filename.
    The path is relative, but it resolves inside the blocked root, and
    the guard resolves before comparing.
    """
    if not PRODUCTION_DATA.exists():
        pytest.skip("no production data directory on this machine")

    monkeypatch.chdir(PRODUCTION_DATA)
    with pytest.raises(ProductionWriteBlocked), open("sneaky_relative_write.txt", "w") as handle:
        handle.write("x")


def test_environment_is_clean_of_a_stray_data_root_override():
    """
    A `$FIELDHORIZON_DATA_ROOT` left set in a developer's shell would
    silently relocate every test's idea of where data lives.
    """
    override = os.environ.get(DATA_ROOT_ENV, "")
    if override:
        assert Path(override).is_absolute()
