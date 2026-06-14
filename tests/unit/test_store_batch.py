"""Unit tests for the store batch runners over an in-memory fake adapter."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from langgraph.store.base import Item
from langgraph.store.memory import InMemoryStore

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import RevisionNotFoundError
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


def test_store_batch_upgrade_noop_when_enumerated_but_already_current():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")  # now at v1

    # An adapter that still enumerates the (now current) item as "stale".
    class _AlwaysStale(InMemoryStoreAdapter):
        def iter_stale_items(self, head):
            yield from self.iter_all_items()

    result = run_store_batch_upgrade(_AlwaysStale(raw), engine(), target="head")

    assert result.total == 1 and result.migrated == 0  # new_env is envelope -> skipped


def test_store_batch_upgrade_raises_on_unknown_revision_by_default():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1, REVISION_METADATA_KEY: "v99"})  # tag absent from registry
    adapter = InMemoryStoreAdapter(raw)

    with pytest.raises(RevisionNotFoundError):
        run_store_batch_upgrade(adapter, engine(), target="head")


def test_store_batch_upgrade_skips_missing_item():
    # store.get returns None for an enumerated key (deleted between enumeration
    # and fetch) -> counted in total but skipped, not crashed on.
    class _GoneStore(InMemoryStore):
        def get(self, namespace, key, *, refresh_ttl=None):
            return None

    class _AdHocAdapter(InMemoryStoreAdapter):
        def iter_stale_items(self, head):
            yield NS, "gone"

    result = run_store_batch_upgrade(_AdHocAdapter(_GoneStore()), engine(), target="head")

    assert result.total == 1 and result.migrated == 0


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


def test_store_batch_downgrade_raises_on_error_by_default():
    raw = InMemoryStore()
    raw.put(NS, "ok", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")
    # Seed an already-upgraded poisoned item; its downgrade raises.
    raw.put(NS, "bad", {"count": 2, "poison": True, "context": {}, REVISION_METADATA_KEY: "v1"})

    with pytest.raises(ValueError, match="poisoned item"):
        run_store_batch_downgrade(adapter, engine(), None)


def test_store_batch_downgrade_raises_on_unknown_revision_by_default():
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1, REVISION_METADATA_KEY: "v99"})  # tag absent from registry
    adapter = InMemoryStoreAdapter(raw)

    with pytest.raises(RevisionNotFoundError):
        run_store_batch_downgrade(adapter, engine(), None)


@pytest.mark.parametrize("policy", ["warn", "pass"])
def test_store_batch_downgrade_skips_unknown_revision_under_policy(policy):
    raw = InMemoryStore()
    raw.put(NS, "a", {"count": 1, REVISION_METADATA_KEY: "v99"})
    adapter = InMemoryStoreAdapter(raw)

    result = run_store_batch_downgrade(adapter, engine(), None, on_unknown_revision=policy)

    assert result.total == 1 and result.migrated == 0 and result.ok
    assert raw.get(NS, "a").value[REVISION_METADATA_KEY] == "v99"  # left untouched


def test_store_batch_downgrade_skips_missing_and_none_value_items():
    # A store that returns None for one key (deleted between enumeration and
    # fetch) and a None-valued Item for another (external/custom store). Both
    # must be skipped, not crashed on.
    raw = InMemoryStore()
    raw.put(NS, "real", {"count": 1})
    adapter = InMemoryStoreAdapter(raw)
    run_store_batch_upgrade(adapter, engine(), target="head")

    class _MixedStore(InMemoryStore):
        def get(self, namespace, key, *, refresh_ttl=None):
            if key == "gone":
                return None
            if key == "nullval":
                return Item(
                    namespace=tuple(namespace),
                    key=key,
                    value=None,
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            return super().get(namespace, key, refresh_ttl=refresh_ttl)

    mixed = _MixedStore()
    mixed.put(NS, "real", raw.get(NS, "real").value)  # already-upgraded item

    class _AdHocAdapter(InMemoryStoreAdapter):
        def iter_all_items(self):
            yield NS, "gone"
            yield NS, "nullval"
            yield NS, "real"

    result = run_store_batch_downgrade(_AdHocAdapter(mixed), engine(), None)

    assert result.total == 3  # all three enumerated
    assert result.migrated == 1  # only the real item reverted; gone/nullval skipped
    assert mixed.get(NS, "real").value == {"count": 1}
