"""Unit tests for the lazy MigrationStore wrapper over an in-memory store."""

from __future__ import annotations

import logging

import pytest
from langgraph.store.memory import InMemoryStore

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import RevisionNotFoundError
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.core.version import ITEM_NAMESPACE_META_KEY
from langmigrate.runtime.store import MigrationStore


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
        return self.rename_field(state, "msgs", "messages")

    def downgrade(self, state):
        return self.rename_field(state, "messages", "msgs")


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1(), V2()]))


NS = ("memories", "user1")


def seed_legacy(raw: InMemoryStore, key: str = "m1") -> None:
    """Persist a v0-style item (no tag, uses 'msgs') directly to the raw store."""
    raw.put(NS, key, {"msgs": ["hi"], "count": 1})


def test_put_stamps_value_in_raw_store():
    raw = InMemoryStore()
    store = MigrationStore(raw, engine())

    store.put(NS, "m1", {"messages": [], "count": 0, "context": {}})

    raw_item = raw.get(NS, "m1")
    assert raw_item.value[REVISION_METADATA_KEY] == "v2"
    # ...but the wrapper never exposes the tag.
    item = store.get(NS, "m1")
    assert REVISION_METADATA_KEY not in item.value


def test_get_migrates_lazily_and_writes_back():
    raw = InMemoryStore()
    seed_legacy(raw)
    store = MigrationStore(raw, engine())

    item = store.get(NS, "m1")
    assert item.value == {"messages": ["hi"], "count": 1, "context": {}}
    assert REVISION_METADATA_KEY not in item.value

    # Healed in place: the raw value is migrated and tagged.
    raw_item = raw.get(NS, "m1")
    assert raw_item.value[REVISION_METADATA_KEY] == "v2"
    assert raw_item.value["messages"] == ["hi"]
    assert "msgs" not in raw_item.value

    # Second read is a no-op view.
    again = store.get(NS, "m1")
    assert again.value == item.value


def test_get_without_write_back_leaves_store_untouched():
    raw = InMemoryStore()
    seed_legacy(raw)
    store = MigrationStore(raw, engine(), write_back=False)

    item = store.get(NS, "m1")
    assert item.value == {"messages": ["hi"], "count": 1, "context": {}}

    raw_item = raw.get(NS, "m1")
    assert raw_item.value == {"msgs": ["hi"], "count": 1}  # still legacy


def test_search_migrates_in_memory_only():
    raw = InMemoryStore()
    seed_legacy(raw, "m1")
    seed_legacy(raw, "m2")
    store = MigrationStore(raw, engine())

    results = store.search(NS)
    assert len(results) == 2
    for item in results:
        assert item.value == {"messages": ["hi"], "count": 1, "context": {}}
        assert REVISION_METADATA_KEY not in item.value

    # Deliberately NOT healed: search would be a write storm.
    for key in ("m1", "m2"):
        raw_item = raw.get(NS, key)
        assert raw_item.value == {"msgs": ["hi"], "count": 1}


def test_delete_passthrough():
    raw = InMemoryStore()
    seed_legacy(raw)
    store = MigrationStore(raw, engine())

    store.delete(NS, "m1")
    assert raw.get(NS, "m1") is None


def test_get_missing_returns_none():
    store = MigrationStore(InMemoryStore(), engine())
    assert store.get(NS, "missing") is None


async def test_async_get_and_put_parity():
    raw = InMemoryStore()
    seed_legacy(raw)
    store = MigrationStore(raw, engine())

    item = await store.aget(NS, "m1")
    assert item.value == {"messages": ["hi"], "count": 1, "context": {}}
    raw_item = raw.get(NS, "m1")
    assert raw_item.value[REVISION_METADATA_KEY] == "v2"

    await store.aput(NS, "m2", {"messages": [], "count": 0, "context": {}})
    assert raw.get(NS, "m2").value[REVISION_METADATA_KEY] == "v2"

    results = await store.asearch(NS)
    assert all(REVISION_METADATA_KEY not in item.value for item in results)


def test_unknown_revision_policy_matrix(caplog):
    raw = InMemoryStore()
    raw.put(NS, "m1", {"messages": ["hi"], REVISION_METADATA_KEY: "deadbeef"})

    with pytest.raises(RevisionNotFoundError):
        MigrationStore(raw, engine()).get(NS, "m1")

    with caplog.at_level(logging.WARNING, logger="langmigrate.runtime"):
        item = MigrationStore(raw, engine(), on_unknown_revision="warn").get(NS, "m1")
    assert item.value == {"messages": ["hi"]}  # served unmigrated, tag stripped
    assert any("unknown revision" in rec.message for rec in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="langmigrate.runtime"):
        item = MigrationStore(raw, engine(), on_unknown_revision="pass").get(NS, "m1")
    assert item.value == {"messages": ["hi"]}
    assert not caplog.records
    # Tolerated reads never write back.
    assert raw.get(NS, "m1").value[REVISION_METADATA_KEY] == "deadbeef"


def test_namespace_dispatch_in_migrations():
    """Migrations can read the item's namespace from the envelope metadata."""

    class NamespaceAware(BaseMigration):
        revision = "ns1"
        down_revision = None

        def upgrade(self, state):
            if state.metadata.get(ITEM_NAMESPACE_META_KEY, ())[:1] == ("memories",):
                return self.add_field(state, "kind", default="memory")
            return state

        def downgrade(self, state):
            return self.drop_field(state, "kind")

    raw = InMemoryStore()
    raw.put(NS, "m1", {"text": "x"})
    raw.put(("other",), "o1", {"text": "y"})
    eng = MigrationEngine(MigrationRegistry.from_migrations([NamespaceAware()]))
    store = MigrationStore(raw, eng)

    assert store.get(NS, "m1").value == {"text": "x", "kind": "memory"}
    assert store.get(("other",), "o1").value == {"text": "y"}


# --- empty registry (no revisions yet) --------------------------------------


def test_empty_registry_is_passthrough():
    raw = InMemoryStore()
    store = MigrationStore(raw, MigrationEngine(MigrationRegistry.from_migrations([])))

    store.put(NS, "m1", {"msgs": ["hi"]})

    # Neither tagged on write nor migrated on read.
    assert raw.get(NS, "m1").value == {"msgs": ["hi"]}
    assert store.get(NS, "m1").value == {"msgs": ["hi"]}


def test_wraps_duck_typed_store_without_ttl_attributes():
    # A custom/duck-typed store that routes through batch but doesn't declare the
    # TTL surface must still be wrappable (real BaseStore subclasses always do).
    # Regression guard: __init__ used to read store.supports_ttl unconditionally.
    from langgraph.store.base import GetOp, Item

    class _MinimalStore:
        # No supports_ttl / ttl_config attributes on purpose.
        def batch(self, ops):
            return [
                Item(
                    namespace=tuple(op.namespace),
                    key=op.key,
                    value={"msgs": ["hi"]},
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                if isinstance(op, GetOp)
                else None
                for op in ops
            ]

        async def abatch(self, ops):
            return self.batch(ops)

    store = MigrationStore(_MinimalStore(), engine(), write_back=False)
    assert store.supports_ttl is False
    assert store.ttl_config is None
    # And it still functions: the legacy item is migrated on read.
    item = store.get(NS, "m1")
    assert item.value == {"messages": ["hi"], "context": {}}
