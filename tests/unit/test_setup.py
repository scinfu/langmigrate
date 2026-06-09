"""Unit tests for ``setup_langmigrate`` and function-pair discovery from disk."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import MigrationEngine, MigrationRegistry, setup_langmigrate
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.runtime.interceptor import MigrationInterceptor

FUNCTION_MIGRATION = """
from langmigrate import migration


@migration("v1", down_revision=None, slug="add_context")
def add_context(state):
    return state.add_field("context", factory=dict)


@add_context.reverse
def _(state):
    return state.drop_field("context")
"""


def _write_migration(path):
    mig_dir = path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "__init__.py").write_text("")
    (mig_dir / "v1_add_context.py").write_text(FUNCTION_MIGRATION)
    return mig_dir


def test_from_path_discovers_function_pair_migration(tmp_path):
    mig_dir = _write_migration(tmp_path)
    registry = MigrationRegistry.from_path(mig_dir)
    assert len(registry) == 1
    assert registry.head() == "v1"
    assert registry.get("v1").is_reversible


def test_setup_langmigrate_from_path(tmp_path):
    mig_dir = _write_migration(tmp_path)
    saver = InMemorySaver()
    interceptor = setup_langmigrate(saver, mig_dir)

    assert isinstance(interceptor, MigrationInterceptor)
    assert interceptor.saver is saver
    assert interceptor.write_back is True

    # End to end: a legacy (untagged) checkpoint is upgraded on load.
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"x": 1}
    chk["channel_versions"] = {"x": 1}
    saver.put(config, chk, {"source": "loop"}, {"x": 1})

    tup = interceptor.get_tuple(config)
    assert tup.checkpoint["channel_values"] == {"x": 1, "context": {}}
    assert tup.metadata[REVISION_METADATA_KEY] == "v1"


def test_setup_langmigrate_accepts_registry_and_engine(tmp_path):
    mig_dir = _write_migration(tmp_path)
    registry = MigrationRegistry.from_path(mig_dir)
    engine = MigrationEngine(registry)
    saver = InMemorySaver()

    from_registry = setup_langmigrate(saver, registry)
    from_engine = setup_langmigrate(saver, engine)

    assert from_registry.engine.head() == "v1"
    assert from_engine.engine is engine


def test_setup_langmigrate_forwards_kwargs(tmp_path):
    mig_dir = _write_migration(tmp_path)
    interceptor = setup_langmigrate(InMemorySaver(), mig_dir, write_back=False, target="v1")
    assert interceptor.write_back is False
    assert interceptor.target == "v1"


def test_setup_langmigrate_str_path(tmp_path):
    mig_dir = _write_migration(tmp_path)
    interceptor = setup_langmigrate(InMemorySaver(), str(mig_dir))
    assert interceptor.engine.head() == "v1"


def test_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        setup_langmigrate(InMemorySaver(), tmp_path / "does_not_exist")
