"""Unit tests for the lazy online MigrationInterceptor over an in-memory saver."""

from __future__ import annotations

from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.runtime.interceptor import MigrationInterceptor


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


def write_legacy_checkpoint(saver: InMemorySaver, thread_id: str) -> dict:
    """Persist a v0-style checkpoint (no tag, uses 'msgs') directly to the saver."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hi"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    return saver.put(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})


def test_lazy_upgrade_on_load():
    saver = InMemorySaver()
    write_legacy_checkpoint(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine())

    tup = interceptor.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
    assert tup is not None
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"


def test_write_back_persists_and_second_load_is_noop():
    saver = InMemorySaver()
    cfg = write_legacy_checkpoint(saver, "t1")
    chk_id = cfg["configurable"]["checkpoint_id"]
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)

    interceptor.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})

    # The raw stored checkpoint is now tagged v2 and migrated, same id preserved.
    raw = saver.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
    assert raw.checkpoint["id"] == chk_id
    assert raw.metadata[REVISION_METADATA_KEY] == "v2"
    assert raw.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}

    # Second load through the interceptor changes nothing further.
    again = interceptor.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
    assert again.checkpoint["id"] == chk_id
    assert again.checkpoint["channel_values"] == raw.checkpoint["channel_values"]


def test_write_back_disabled_does_not_touch_db():
    saver = InMemorySaver()
    write_legacy_checkpoint(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine(), write_back=False)

    interceptor.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
    raw = saver.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
    # DB untouched: still legacy.
    assert raw.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}
    assert REVISION_METADATA_KEY not in raw.metadata


def test_put_stamps_head_revision():
    saver = InMemorySaver()
    interceptor = MigrationInterceptor(saver, engine())
    config = {"configurable": {"thread_id": "t2", "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"messages": [], "count": 0, "context": {}}
    interceptor.put(config, chk, {"source": "input"}, {})

    raw = saver.get_tuple({"configurable": {"thread_id": "t2", "checkpoint_ns": ""}})
    assert raw.metadata[REVISION_METADATA_KEY] == "v2"


class CoerceToFloat(BaseMigration):
    revision = "c1"
    down_revision = None

    def upgrade(self, state):
        # A genuine type change whose old/new values compare equal (1 == 1.0).
        return self.coerce_field(state, "score", float)

    def downgrade(self, state):
        return self.coerce_field(state, "score", int)


def test_write_back_persists_type_only_coercion():
    # Regression: a coercion like 1 -> 1.0 is a real change, but old == new under
    # plain equality. The version reconciliation must still bump the channel so the
    # new blob is written back — otherwise the checkpoint is stamped as migrated
    # while the stored value stays the un-coerced int (silent data loss).
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"score": 1}
    chk["channel_versions"] = {"score": 1}
    saver.put(cfg, chk, {"source": "loop"}, {"score": 1})

    eng = MigrationEngine(MigrationRegistry.from_migrations([CoerceToFloat()]))
    interceptor = MigrationInterceptor(saver, eng, write_back=True)
    interceptor.get_tuple(cfg)

    raw = saver.get_tuple(cfg)
    assert raw.metadata[REVISION_METADATA_KEY] == "c1"
    stored = raw.checkpoint["channel_values"]["score"]
    assert stored == 1.0
    assert type(stored) is float


def test_list_migrates_view_but_does_not_write_back():
    saver = InMemorySaver()
    write_legacy_checkpoint(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)

    # The listed (in-memory) view is migrated...
    listed = list(interceptor.list({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}))
    assert listed
    assert listed[0].checkpoint["channel_values"] == {
        "messages": ["hi"],
        "count": 1,
        "context": {},
    }
    assert listed[0].metadata[REVISION_METADATA_KEY] == "v2"

    # ...but the underlying DB is deliberately left legacy (no write storm).
    # Inspect the stored rows directly via the raw saver (no migration on its path).
    raw_rows = list(saver.list({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}))
    assert raw_rows[0].checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}
    assert REVISION_METADATA_KEY not in raw_rows[0].metadata


async def _aseed_legacy(saver: InMemorySaver, thread_id: str) -> str:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hi"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    cfg = await saver.aput(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})
    return cfg["configurable"]["checkpoint_id"]


async def test_async_lazy_upgrade_and_write_back():
    saver = InMemorySaver()
    chk_id = await _aseed_legacy(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}

    tup = await interceptor.aget_tuple(config)
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"

    # Async write-back persisted to the DB, same id preserved.
    raw = await saver.aget_tuple(config)
    assert raw.checkpoint["id"] == chk_id
    assert raw.metadata[REVISION_METADATA_KEY] == "v2"
    assert raw.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}


async def test_async_aput_stamps_head():
    saver = InMemorySaver()
    interceptor = MigrationInterceptor(saver, engine())
    config = {"configurable": {"thread_id": "t2", "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"messages": [], "count": 0, "context": {}}
    await interceptor.aput(config, chk, {"source": "input"}, {})

    raw = await saver.aget_tuple(config)
    assert raw.metadata[REVISION_METADATA_KEY] == "v2"


async def test_async_alist_migrates_view_without_write_back():
    saver = InMemorySaver()
    await _aseed_legacy(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}

    listed = [tup async for tup in interceptor.alist(config)]
    assert listed[0].checkpoint["channel_values"] == {
        "messages": ["hi"],
        "count": 1,
        "context": {},
    }
    # DB left legacy: alist never writes back.
    raw_rows = [tup async for tup in saver.alist(config)]
    assert raw_rows[0].checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}
    assert REVISION_METADATA_KEY not in raw_rows[0].metadata


def test_already_current_load_is_noop():
    saver = InMemorySaver()
    interceptor = MigrationInterceptor(saver, engine())
    config = {"configurable": {"thread_id": "t3", "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"messages": ["x"], "count": 2, "context": {"a": 1}}
    chk["channel_versions"] = {"messages": 1, "count": 1, "context": 1}
    interceptor.put(config, chk, {"source": "loop"}, {"messages": 1, "count": 1, "context": 1})

    tup = interceptor.get_tuple(config)
    assert tup.checkpoint["channel_values"] == {"messages": ["x"], "count": 2, "context": {"a": 1}}


def test_get_tuple_returns_none_when_checkpoint_absent():
    # No checkpoint for the thread → the interceptor passes the None straight
    # through (nothing to migrate or write back).
    saver = InMemorySaver()
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)
    assert (
        interceptor.get_tuple({"configurable": {"thread_id": "missing", "checkpoint_ns": ""}})
        is None
    )


async def test_async_get_tuple_returns_none_when_checkpoint_absent():
    saver = InMemorySaver()
    interceptor = MigrationInterceptor(saver, engine(), write_back=True)
    result = await interceptor.aget_tuple(
        {"configurable": {"thread_id": "missing", "checkpoint_ns": ""}}
    )
    assert result is None


def test_put_writes_delegates_to_wrapped_saver():
    saver = InMemorySaver()
    cfg = write_legacy_checkpoint(saver, "t1")
    interceptor = MigrationInterceptor(saver, engine())

    interceptor.put_writes(cfg, [("messages", ["w"])], task_id="task-1")
    # The pending write surfaces on the wrapped saver's tuple.
    raw = saver.get_tuple(cfg)
    assert raw is not None
    assert raw.pending_writes  # (task_id, channel, value) entries recorded


async def test_async_put_writes_delegates_to_wrapped_saver():
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hi"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    cfg = await saver.aput(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})
    interceptor = MigrationInterceptor(saver, engine())

    await interceptor.aput_writes(cfg, [("messages", ["w"])], task_id="task-1")
    raw = await saver.aget_tuple(cfg)
    assert raw is not None
    assert raw.pending_writes
