"""End-to-end integration test for the Redis adapter + batch/online migration.

Requires Docker Redis Stack (``docker compose up -d``) and the ``[redis]`` extra.
Connection string via ``LANGMIGRATE_TEST_REDIS`` env or the docker-compose default.
Run with: ``uv run pytest -m integration``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import REVISION_METADATA_KEY
from langmigrate.runtime.batch import run_batch_downgrade, run_batch_upgrade
from langmigrate.runtime.interceptor import MigrationInterceptor

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("LANGMIGRATE_TEST_REDIS", "redis://localhost:6389")


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


@pytest.fixture
def adapter():
    pytest.importorskip("redis")
    pytest.importorskip("langgraph.checkpoint.redis")
    from langmigrate.adapters.redis import RedisAdapter

    try:
        ad = RedisAdapter.from_conn_string(REDIS_URL)
        ad.saver._redis.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis not reachable: {exc}")
    yield ad
    ad.close()


def _seed_legacy(saver, thread_id: str):
    from langgraph.checkpoint.base import empty_checkpoint

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hi"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    return saver.put(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})


def _raw_tuple(adapter, thread_id: str):
    """Read a checkpoint straight from the saver (bypassing any interceptor)."""
    return adapter.saver.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})


def test_lazy_online_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)

    tup = interceptor.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"


def test_batch_upgrade_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)

    # At least the just-seeded checkpoint is stale before the run.
    assert adapter.count_stale("v2") >= 1
    result = run_batch_upgrade(adapter, engine(), target="head")
    assert result.migrated >= 1

    tup = adapter.saver.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}


def test_batch_downgrade_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    run_batch_upgrade(adapter, engine(), target="head")  # -> v2
    result = run_batch_downgrade(adapter, engine(), "v1")  # undo v2 (rename) only
    assert result.migrated >= 1

    tup = adapter.saver.get_tuple(config)
    assert tup.metadata[REVISION_METADATA_KEY] == "v1"
    # v2 rename reverted: back to `msgs`; context (from v1) still present.
    assert tup.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1, "context": {}}

    run_batch_upgrade(adapter, engine(), target="head")  # restore, don't leave stale


def test_list_does_not_write_back_redis_e2e(adapter):
    # CLAUDE.md decision #3: list() migrates in memory only, never writing back.
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)

    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    listed = list(interceptor.list(cfg))
    assert listed, "expected at least the seeded checkpoint"
    assert listed[0].metadata[REVISION_METADATA_KEY] == "v2"
    assert listed[0].checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}

    # The stored doc is untouched: still untagged and still legacy `msgs`.
    raw = _raw_tuple(adapter, thread_id)
    assert REVISION_METADATA_KEY not in (raw.metadata or {})
    assert raw.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    run_batch_upgrade(adapter, engine(), target="head")  # restore


def test_online_dry_run_does_not_write_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)

    result = run_batch_upgrade(adapter, engine(), target="head", dry_run=True)
    assert result.dry_run is True
    assert result.migrated >= 1

    raw = _raw_tuple(adapter, thread_id)
    assert REVISION_METADATA_KEY not in (raw.metadata or {})
    assert raw.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    run_batch_upgrade(adapter, engine(), target="head")  # restore


def test_idempotent_reread_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg = _seed_legacy(adapter.saver, thread_id)
    chk_id = cfg["configurable"]["checkpoint_id"]
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)
    thread_cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    first = interceptor.get_tuple(thread_cfg)
    second = interceptor.get_tuple(thread_cfg)
    assert first.checkpoint["id"] == chk_id
    assert second.checkpoint["id"] == chk_id
    assert second.metadata[REVISION_METADATA_KEY] == "v2"
    assert second.checkpoint["channel_values"] == first.checkpoint["channel_values"]


def test_parent_chain_preserved_on_writeback_redis_e2e(adapter):
    # Two checkpoints (parent -> child); write-back of the child must preserve its
    # id and parent pointer (CLAUDE.md decision #3).
    from langgraph.checkpoint.base import empty_checkpoint

    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg0 = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    chk1 = empty_checkpoint()
    chk1["channel_values"] = {"msgs": ["a"], "count": 1}
    chk1["channel_versions"] = {"msgs": 1, "count": 1}
    cfg1 = adapter.saver.put(cfg0, chk1, {"source": "loop"}, {"msgs": 1, "count": 1})
    id1 = cfg1["configurable"]["checkpoint_id"]

    chk2 = empty_checkpoint()
    chk2["channel_values"] = {"msgs": ["a", "b"], "count": 2}
    chk2["channel_versions"] = {"msgs": 2, "count": 2}
    cfg2 = adapter.saver.put(cfg1, chk2, {"source": "loop"}, {"msgs": 2, "count": 2})
    id2 = cfg2["configurable"]["checkpoint_id"]
    assert id1 != id2

    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)
    migrated = interceptor.get_tuple(cfg0)  # latest = child (id2)
    assert migrated.checkpoint["id"] == id2
    assert migrated.metadata[REVISION_METADATA_KEY] == "v2"
    assert migrated.parent_config is not None
    assert migrated.parent_config["configurable"]["checkpoint_id"] == id1
    parent = adapter.saver.get_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": id1}}
    )
    assert parent is not None
    assert parent.checkpoint["id"] == id1


def test_stamp_and_revision_counts_redis_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    updated = adapter.stamp_all("v2")  # tag without migrating data
    assert updated >= 1

    tup = adapter.saver.get_tuple(config)
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"
    # Data is NOT migrated by stamp — still legacy `msgs`.
    assert tup.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    assert adapter.revision_counts().get("v2", 0) >= 1
