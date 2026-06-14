"""Unit tests for the store batch runners over an in-memory fake adapter."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langgraph.store.memory import InMemoryStore

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.core.version import read_value_revision
from langmigrate.runtime.batch import run_store_batch_downgrade, run_store_batch_upgrade


class V1(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        if state.values.get("poison"):
            raise ValueError("poisoned item")
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        if state.values.get("poison"):
            raise ValueError("poisoned item")
        return self.drop_field(state, "context")


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1()]))


class InMemoryStoreAdapter:
    """Minimal StoreAdapter over InMemoryStore for testing the batch runners."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    @property
    def store(self) -> InMemoryStore:
        return self._store

    def _all_items(self):
        # InMemoryStore keeps items in a nested dict {namespace: {key: Item}}.
        for namespace, items in self._store._data.items():
            for key, item in items.items():
                yield namespace, key, item

    def iter_stale_items(self, head: str) -> Iterator[tuple[tuple[str, ...], str]]:
        for namespace, key, item in self._all_items():
            if read_value_revision(item.value) != head:
                yield namespace, key

    def iter_all_items(self) -> Iterator[tuple[tuple[str, ...], str]]:
        for namespace, key, _ in self._all_items():
            yield namespace, key

    def revision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, _, item in self._all_items():
            rev = read_value_revision(item.value) or "<untagged>"
            counts[rev] = counts.get(rev, 0) + 1
        return counts

    def stamp_all(self, revision: str) -> int:
        count = 0
        for namespace, key, item in self._all_items():
            self._store.put(namespace, key, {**item.value, REVISION_METADATA_KEY: revision})
            count += 1
        return count


NS = ("memories", "u1")


def test_store_batch_upgrade_migrates_all_stale():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    raw.put(NS, "b", {"count": 2})
    adapter = InMemoryStoreAdapter(raw)

    result = run_store_batch_upgrade(adapter, engine(), target="head")

    assert result.total == 2
    assert result.migrated == 2
    for key in ("a", "b"):
        value = raw.get(NS, key).value
        assert value[REVISION_METADATA_KEY] == "v1"
        assert value["context"] == {}


def test_store_batch_upgrade_dry_run_validates_without_writing():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)

    result = run_store_batch_upgrade(adapter, engine(), target="head", dry_run=True)
    assert result.dry_run and result.migrated == 1
    assert REVISION_METADATA_KEY not in raw.get(NS, "a").value

    raw.put(NS, "bad", {"poison": True})
    with pytest.raises(ValueError, match="poisoned"):
        run_store_batch_upgrade(adapter, engine(), target="head", dry_run=True)


def test_store_batch_upgrade_continue_on_error():
    raw = InMemoryStore()
    raw.put(NS, "ok", {"count": 1})
    raw.put(NS, "bad", {"poison": True})
    adapter = InMemoryStoreAdapter(raw)

    result = run_store_batch_upgrade(adapter, engine(), target="head", continue_on_error=True)

    assert result.total == 2
    assert result.migrated == 1
    assert result.failed == 1
    (failure,) = result.failures
    assert failure.ref == "memories/u1:bad"
    assert failure.error_type == "ValueError"
    assert raw.get(NS, "ok").value[REVISION_METADATA_KEY] == "v1"
    assert REVISION_METADATA_KEY not in raw.get(NS, "bad").value


def test_store_batch_rerun_is_noop():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")

    second = run_store_batch_upgrade(adapter, engine(), target="head")
    assert second.total == 0
    assert second.migrated == 0


def test_store_batch_downgrade_to_base_reverts_and_untags():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")

    result = run_store_batch_downgrade(adapter, engine(), None)

    assert result.target == "base"
    assert result.migrated == 1
    value = raw.get(NS, "a").value
    assert value == {"count": 1}  # context dropped, tag removed


def test_store_batch_downgrade_skips_untagged():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})  # never upgraded
    adapter = InMemoryStoreAdapter(raw)

    result = run_store_batch_downgrade(adapter, engine(), None)

    assert result.total == 1
    assert result.migrated == 0
    assert raw.get(NS, "a").value == {"count": 1}


def test_store_batch_downgrade_dry_run_validates_without_writing():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")

    result = run_store_batch_downgrade(adapter, engine(), None, dry_run=True)

    assert result.dry_run is True
    assert result.migrated == 1  # counted as would-migrate
    # ...but the store is untouched: still tagged and still carrying `context`.
    value = raw.get(NS, "a").value
    assert value[REVISION_METADATA_KEY] == "v1"
    assert value["context"] == {}


def test_store_batch_downgrade_to_same_revision_is_noop():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")

    # Downgrade target == current revision: nothing to reverse (new_env is envelope).
    result = run_store_batch_downgrade(adapter, engine(), "v1")

    assert result.total == 1
    assert result.migrated == 0
    assert raw.get(NS, "a").value[REVISION_METADATA_KEY] == "v1"


def test_store_batch_downgrade_continue_on_error():
    raw = InMemoryStore()
    raw.put(NS, "ok", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")
    # Seed an already-upgraded poisoned item so only its *downgrade* fails.
    raw.put(NS, "bad", {"count": 2, "poison": True, "context": {}, REVISION_METADATA_KEY: "v1"})

    result = run_store_batch_downgrade(adapter, engine(), None, continue_on_error=True)

    assert result.failed == 1
    assert result.failures[0].error_type == "ValueError"
    assert not result.ok
    # The healthy item was still reverted despite the poisoned one failing.
    assert raw.get(NS, "ok").value == {"count": 1}
