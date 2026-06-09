"""Unit tests for the batch migration runner using an in-memory fake adapter."""

from __future__ import annotations

from collections.abc import Iterator

from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.core.version import read_revision
from langmigrate.runtime.batch import run_batch_downgrade, run_batch_upgrade


class V1(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        return self.drop_field(state, "context")


class V2(BaseMigration):
    revision = "v2"
    down_revision = "v1"

    def upgrade(self, state):
        return self.add_field(state, "tags", factory=list)

    def downgrade(self, state):
        return self.drop_field(state, "tags")


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1()]))


def engine2() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1(), V2()]))


class InMemoryAdapter:
    """Minimal CheckpointAdapter over InMemorySaver for testing the batch runner."""

    def __init__(self, saver: InMemorySaver) -> None:
        self._saver = saver

    @property
    def saver(self) -> InMemorySaver:
        return self._saver

    def _all_tuples(self):
        return list(self._saver.list(None))

    def count_stale(self, head: str) -> int:
        return sum(1 for t in self._all_tuples() if read_revision(t.metadata) != head)

    def iter_stale_configs(self, head: str) -> Iterator[dict]:
        for t in self._all_tuples():
            if read_revision(t.metadata) != head:
                yield t.config

    def iter_all_configs(self) -> Iterator[dict]:
        for t in self._all_tuples():
            yield t.config


def seed(saver: InMemorySaver, thread_id: str) -> None:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"count": 1}
    chk["channel_versions"] = {"count": 1}
    saver.put(config, chk, {"source": "loop"}, {"count": 1})


def test_batch_upgrade_migrates_all_stale():
    saver = InMemorySaver()
    seed(saver, "a")
    seed(saver, "b")
    adapter = InMemoryAdapter(saver)

    result = run_batch_upgrade(adapter, engine(), target="head")

    assert result.total == 2
    assert result.migrated == 2
    assert not result.dry_run
    for t in saver.list(None):
        assert t.metadata[REVISION_METADATA_KEY] == "v1"
        assert t.checkpoint["channel_values"] == {"count": 1, "context": {}}


def test_batch_dry_run_does_not_write():
    saver = InMemorySaver()
    seed(saver, "a")
    adapter = InMemoryAdapter(saver)

    result = run_batch_upgrade(adapter, engine(), target="head", dry_run=True)

    assert result.dry_run
    assert result.total == 1
    assert result.migrated == 1
    (t,) = list(saver.list(None))
    assert REVISION_METADATA_KEY not in t.metadata  # untouched


def test_batch_rerun_is_noop_after_migration():
    saver = InMemorySaver()
    seed(saver, "a")
    adapter = InMemoryAdapter(saver)
    run_batch_upgrade(adapter, engine(), target="head")

    second = run_batch_upgrade(adapter, engine(), target="head")
    assert second.total == 0
    assert second.migrated == 0


def test_batch_downgrade_to_base_reverts_and_untags():
    saver = InMemorySaver()
    seed(saver, "a")
    seed(saver, "b")
    adapter = InMemoryAdapter(saver)
    run_batch_upgrade(adapter, engine(), target="head")  # -> v1, adds context

    result = run_batch_downgrade(adapter, engine(), None)  # down to base

    assert result.target == "base"
    assert result.total == 2  # scanned all checkpoints
    assert result.migrated == 2
    assert not result.dry_run
    for t in saver.list(None):
        assert "context" not in t.checkpoint["channel_values"]
        assert read_revision(t.metadata) is None


def test_batch_downgrade_to_specific_target():
    saver = InMemorySaver()
    seed(saver, "a")
    adapter = InMemoryAdapter(saver)
    run_batch_upgrade(adapter, engine2(), target="head")  # -> v2 (context + tags)

    result = run_batch_downgrade(adapter, engine2(), "v1")  # only undo v2

    assert result.target == "v1"
    assert result.migrated == 1
    (t,) = list(saver.list(None))
    assert t.checkpoint["channel_values"] == {"count": 1, "context": {}}
    assert read_revision(t.metadata) == "v1"


def test_batch_downgrade_dry_run_does_not_write():
    saver = InMemorySaver()
    seed(saver, "a")
    adapter = InMemoryAdapter(saver)
    run_batch_upgrade(adapter, engine(), target="head")

    result = run_batch_downgrade(adapter, engine(), None, dry_run=True)

    assert result.dry_run
    assert result.migrated == 1
    (t,) = list(saver.list(None))
    # Still upgraded: dry-run did not revert.
    assert t.checkpoint["channel_values"] == {"count": 1, "context": {}}
    assert read_revision(t.metadata) == "v1"


def test_batch_downgrade_skips_untagged():
    saver = InMemorySaver()
    seed(saver, "a")  # untagged, never upgraded
    adapter = InMemoryAdapter(saver)

    result = run_batch_downgrade(adapter, engine(), None)

    assert result.total == 1  # scanned, but...
    assert result.migrated == 0  # ...nothing to revert (untagged)
    (t,) = list(saver.list(None))
    assert t.checkpoint["channel_values"] == {"count": 1}


def test_batch_downgrade_to_current_revision_is_noop():
    # Downgrading every checkpoint to the revision it already carries changes
    # nothing (each downgrade_state returns the same envelope).
    saver = InMemorySaver()
    seed(saver, "a")
    adapter = InMemoryAdapter(saver)
    run_batch_upgrade(adapter, engine(), target="head")  # -> v1

    result = run_batch_downgrade(adapter, engine(), "v1")  # already at v1

    assert result.total == 1  # scanned
    assert result.migrated == 0  # nothing to revert
    (t,) = list(saver.list(None))
    assert read_revision(t.metadata) == "v1"
    assert t.checkpoint["channel_values"] == {"count": 1, "context": {}}
