"""Unit tests for the batch migration runner using an in-memory fake adapter."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import RevisionNotFoundError
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


# -- error tolerance & validating dry-run -------------------------------------


class Poison(BaseMigration):
    """Fails on checkpoints whose `count` is the poison value."""

    revision = "p1"
    down_revision = None

    def upgrade(self, state):
        if state.values.get("count") == 666:
            raise ValueError("poisoned checkpoint")
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        if state.values.get("count") == 666:
            raise ValueError("poisoned checkpoint")
        return self.drop_field(state, "context")


def seed_count(saver: InMemorySaver, thread_id: str, count: int) -> None:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"count": count}
    chk["channel_versions"] = {"count": 1}
    saver.put(config, chk, {"source": "loop"}, {"count": 1})


def poison_engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([Poison()]))


def test_batch_upgrade_aborts_on_error_by_default():
    import pytest

    saver = InMemorySaver()
    seed_count(saver, "bad", 666)
    adapter = InMemoryAdapter(saver)

    with pytest.raises(ValueError, match="poisoned"):
        run_batch_upgrade(adapter, poison_engine(), target="head")


def test_batch_upgrade_continue_on_error_collects_failures():
    saver = InMemorySaver()
    seed_count(saver, "ok-1", 1)
    seed_count(saver, "bad", 666)
    seed_count(saver, "ok-2", 2)
    adapter = InMemoryAdapter(saver)

    result = run_batch_upgrade(adapter, poison_engine(), target="head", continue_on_error=True)

    assert result.total == 3
    assert result.migrated == 2
    assert result.failed == 1
    assert not result.ok
    (failure,) = result.failures
    assert failure.ref.startswith("bad/")
    assert failure.error_type == "ValueError"
    assert "poisoned" in failure.error
    # The healthy checkpoints were migrated despite the failure.
    healed = {
        t.config["configurable"]["thread_id"]: read_revision(t.metadata) for t in saver.list(None)
    }
    assert healed["ok-1"] == "p1"
    assert healed["ok-2"] == "p1"
    assert healed["bad"] is None


def test_batch_dry_run_validates_migrations():
    # A dry run executes the cascade in memory: a broken migration must surface
    # instead of being silently counted as "would migrate".
    import pytest

    saver = InMemorySaver()
    seed_count(saver, "bad", 666)
    adapter = InMemoryAdapter(saver)

    with pytest.raises(ValueError, match="poisoned"):
        run_batch_upgrade(adapter, poison_engine(), target="head", dry_run=True)


def test_batch_upgrade_does_not_use_count_stale():
    # The runner enumerates once; count_stale stays on the protocol for
    # compatibility but must not be called (Redis would pay a second full scan).
    class CountingAdapter(InMemoryAdapter):
        count_stale_calls = 0

        def count_stale(self, head: str) -> int:
            type(self).count_stale_calls += 1
            return super().count_stale(head)

    saver = InMemorySaver()
    seed(saver, "a")
    adapter = CountingAdapter(saver)

    run_batch_upgrade(adapter, engine(), target="head")
    assert CountingAdapter.count_stale_calls == 0


def test_batch_upgrade_skips_missing_tuple():
    ghost = {"configurable": {"thread_id": "ghost", "checkpoint_ns": "", "checkpoint_id": "x"}}

    class _GhostAdapter(InMemoryAdapter):
        def iter_stale_configs(self, head):
            yield ghost

    result = run_batch_upgrade(_GhostAdapter(InMemorySaver()), engine(), target="head")

    assert result.total == 1 and result.migrated == 0  # get_tuple -> None -> skipped


def test_batch_upgrade_noop_when_enumerated_but_already_current():
    saver = InMemorySaver()
    seed(saver, "a")
    run_batch_upgrade(InMemoryAdapter(saver), engine(), target="head")  # now at v1

    # An adapter that still enumerates the (now current) config as "stale".
    class _AlwaysStale(InMemoryAdapter):
        def iter_stale_configs(self, head):
            yield from self.iter_all_configs()

    result = run_batch_upgrade(_AlwaysStale(saver), engine(), target="head")

    assert result.total == 1 and result.migrated == 0  # _plan_upgrade -> None (no-op)


def test_batch_downgrade_continue_on_error_collects_failures():
    saver = InMemorySaver()
    seed_count(saver, "ok", 1)
    seed_count(saver, "bad", 666)
    adapter = InMemoryAdapter(saver)
    # Manually stamp both as p1 so the downgrade path runs (bad would fail upgrade).
    for t in list(saver.list(None)):
        meta = dict(t.metadata or {})
        meta[REVISION_METADATA_KEY] = "p1"
        saver.put(t.config, t.checkpoint, meta, {})

    result = run_batch_downgrade(adapter, poison_engine(), None, continue_on_error=True)

    assert result.failed == 1
    assert result.failures[0].error_type == "ValueError"
    healed = {
        t.config["configurable"]["thread_id"]: read_revision(t.metadata) for t in saver.list(None)
    }
    assert healed["ok"] is None  # downgraded past base -> untagged
    assert healed["bad"] == "p1"  # left as-is, recorded as failure


def test_batch_downgrade_skips_missing_tuple():
    ghost = {"configurable": {"thread_id": "ghost", "checkpoint_ns": "", "checkpoint_id": "x"}}

    class _GhostAdapter(InMemoryAdapter):
        def iter_all_configs(self):
            yield ghost

    result = run_batch_downgrade(_GhostAdapter(InMemorySaver()), engine(), None)

    assert result.total == 1 and result.migrated == 0  # get_tuple -> None -> skipped


def test_batch_downgrade_raises_on_unknown_revision_by_default():
    import pytest

    saver = InMemorySaver()
    seed(saver, "a")
    # Stamp with a revision absent from the registry (code-rollback case).
    (t,) = list(saver.list(None))
    meta = dict(t.metadata or {})
    meta[REVISION_METADATA_KEY] = "v99"
    saver.put(t.config, t.checkpoint, meta, {})
    adapter = InMemoryAdapter(saver)

    with pytest.raises(RevisionNotFoundError):
        run_batch_downgrade(adapter, engine(), None)


@pytest.mark.parametrize("policy", ["warn", "pass"])
def test_batch_downgrade_skips_unknown_revision_under_policy(policy):
    saver = InMemorySaver()
    seed(saver, "a")
    (t,) = list(saver.list(None))
    meta = dict(t.metadata or {})
    meta[REVISION_METADATA_KEY] = "v99"
    saver.put(t.config, t.checkpoint, meta, {})
    adapter = InMemoryAdapter(saver)

    result = run_batch_downgrade(adapter, engine(), None, on_unknown_revision=policy)

    assert result.total == 1 and result.migrated == 0 and result.ok
    assert read_revision(next(iter(saver.list(None))).metadata) == "v99"  # untouched


# -- async runners -------------------------------------------------------------


class AsyncInMemoryAdapter:
    """Async adapter over InMemorySaver (it implements the async saver API)."""

    def __init__(self, saver: InMemorySaver) -> None:
        self._saver = saver

    @property
    def saver(self) -> InMemorySaver:
        return self._saver

    async def aiter_stale_configs(self, head: str):
        for t in [tup async for tup in self._saver.alist(None)]:
            if read_revision(t.metadata) != head:
                yield t.config

    async def aiter_all_configs(self):
        for t in [tup async for tup in self._saver.alist(None)]:
            yield t.config


async def test_async_batch_upgrade_migrates_all_stale():
    from langmigrate.runtime.batch import arun_batch_upgrade

    saver = InMemorySaver()
    seed(saver, "a")
    seed(saver, "b")
    adapter = AsyncInMemoryAdapter(saver)

    result = await arun_batch_upgrade(adapter, engine(), target="head")

    assert result.total == 2
    assert result.migrated == 2
    for t in saver.list(None):
        assert t.metadata[REVISION_METADATA_KEY] == "v1"
        assert t.checkpoint["channel_values"] == {"count": 1, "context": {}}


async def test_async_batch_upgrade_dry_run_validates_without_writing():
    import pytest

    from langmigrate.runtime.batch import arun_batch_upgrade

    saver = InMemorySaver()
    seed(saver, "a")
    adapter = AsyncInMemoryAdapter(saver)

    result = await arun_batch_upgrade(adapter, engine(), target="head", dry_run=True)
    assert result.dry_run and result.migrated == 1
    (t,) = list(saver.list(None))
    assert REVISION_METADATA_KEY not in t.metadata  # untouched

    # And a broken migration surfaces during the dry run.
    seed_count(saver, "bad", 666)
    with pytest.raises(ValueError, match="poisoned"):
        await arun_batch_upgrade(adapter, poison_engine(), target="head", dry_run=True)


async def test_async_batch_upgrade_continue_on_error():
    from langmigrate.runtime.batch import arun_batch_upgrade

    saver = InMemorySaver()
    seed_count(saver, "ok", 1)
    seed_count(saver, "bad", 666)
    adapter = AsyncInMemoryAdapter(saver)

    result = await arun_batch_upgrade(
        adapter, poison_engine(), target="head", continue_on_error=True
    )
    assert result.failed == 1
    assert result.migrated == 1
    assert result.failures[0].error_type == "ValueError"


async def test_async_batch_upgrade_skips_missing_tuple():
    from langmigrate.runtime.batch import arun_batch_upgrade

    ghost = {"configurable": {"thread_id": "ghost", "checkpoint_ns": "", "checkpoint_id": "g"}}

    class _GhostAdapter:
        def __init__(self, saver: InMemorySaver) -> None:
            self._saver = saver

        @property
        def saver(self) -> InMemorySaver:
            return self._saver

        async def aiter_stale_configs(self, head):
            yield ghost

    result = await arun_batch_upgrade(_GhostAdapter(InMemorySaver()), engine(), target="head")

    assert result.total == 1 and result.migrated == 0  # aget_tuple -> None -> skipped


async def test_async_batch_upgrade_noop_when_enumerated_but_already_current():
    from langmigrate.runtime.batch import arun_batch_upgrade

    saver = InMemorySaver()
    seed(saver, "a")
    await arun_batch_upgrade(AsyncInMemoryAdapter(saver), engine(), target="head")  # now at v1

    class _AlwaysStale(AsyncInMemoryAdapter):
        async def aiter_stale_configs(self, head):
            async for cfg in self.aiter_all_configs():
                yield cfg

    result = await arun_batch_upgrade(_AlwaysStale(saver), engine(), target="head")

    assert result.total == 1 and result.migrated == 0  # _plan_upgrade -> None (no-op)


async def test_async_batch_downgrade_to_base():
    from langmigrate.runtime.batch import arun_batch_downgrade, arun_batch_upgrade

    saver = InMemorySaver()
    seed(saver, "a")
    adapter = AsyncInMemoryAdapter(saver)
    await arun_batch_upgrade(adapter, engine(), target="head")

    result = await arun_batch_downgrade(adapter, engine(), None)

    assert result.target == "base"
    assert result.migrated == 1
    (t,) = list(saver.list(None))
    assert "context" not in t.checkpoint["channel_values"]
    assert read_revision(t.metadata) is None


async def test_async_batch_downgrade_dry_run_does_not_write():
    from langmigrate.runtime.batch import arun_batch_downgrade, arun_batch_upgrade

    saver = InMemorySaver()
    seed(saver, "a")
    adapter = AsyncInMemoryAdapter(saver)
    await arun_batch_upgrade(adapter, engine(), target="head")

    result = await arun_batch_downgrade(adapter, engine(), None, dry_run=True)

    assert result.dry_run and result.migrated == 1
    (t,) = list(saver.list(None))
    assert "context" in t.checkpoint["channel_values"]  # untouched
    assert read_revision(t.metadata) == "v1"  # still tagged


async def test_async_batch_downgrade_skips_untagged():
    from langmigrate.runtime.batch import arun_batch_downgrade

    saver = InMemorySaver()
    seed(saver, "a")  # never upgraded -> untagged, nothing to reverse
    adapter = AsyncInMemoryAdapter(saver)

    result = await arun_batch_downgrade(adapter, engine(), None)

    assert result.total == 1 and result.migrated == 0


async def test_async_batch_downgrade_skips_missing_tuple():
    from langmigrate.runtime.batch import arun_batch_downgrade

    # A config the saver can't resolve (aget_tuple -> None).
    ghost = {"configurable": {"thread_id": "ghost", "checkpoint_ns": "", "checkpoint_id": "g"}}

    class _GhostAdapter:
        def __init__(self, saver: InMemorySaver) -> None:
            self._saver = saver

        @property
        def saver(self) -> InMemorySaver:
            return self._saver

        async def aiter_all_configs(self):
            yield ghost

    result = await arun_batch_downgrade(_GhostAdapter(InMemorySaver()), engine(), None)

    assert result.total == 1 and result.migrated == 0


async def test_async_batch_downgrade_continue_on_error():
    from langmigrate.runtime.batch import arun_batch_downgrade

    saver = InMemorySaver()
    seed_count(saver, "ok", 1)
    seed_count(saver, "bad", 666)
    # Stamp both as p1 so the downgrade path runs (bad would otherwise fail upgrade).
    for t in list(saver.list(None)):
        meta = dict(t.metadata or {})
        meta[REVISION_METADATA_KEY] = "p1"
        saver.put(t.config, t.checkpoint, meta, {})
    adapter = AsyncInMemoryAdapter(saver)

    result = await arun_batch_downgrade(adapter, poison_engine(), None, continue_on_error=True)

    assert result.failed == 1
    assert result.failures[0].error_type == "ValueError"
    assert not result.ok


async def test_async_batch_downgrade_aborts_on_error_by_default():
    from langmigrate.runtime.batch import arun_batch_downgrade

    saver = InMemorySaver()
    seed_count(saver, "bad", 666)
    (t,) = list(saver.list(None))
    meta = dict(t.metadata or {})
    meta[REVISION_METADATA_KEY] = "p1"
    saver.put(t.config, t.checkpoint, meta, {})
    adapter = AsyncInMemoryAdapter(saver)

    with pytest.raises(ValueError, match="poisoned"):
        await arun_batch_downgrade(adapter, poison_engine(), None)
