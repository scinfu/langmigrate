"""End-to-end integration test for the Postgres adapter + batch/online migration.

Requires Docker Postgres (``docker compose up -d``) and the ``[postgres]`` extra.
Connection string via ``LANGMIGRATE_TEST_PG`` env or the docker-compose default.
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

PG = os.environ.get(
    "LANGMIGRATE_TEST_PG",
    "postgresql://langmigrate:langmigrate@localhost:5442/langmigrate",
)


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
    pytest.importorskip("psycopg")
    pytest.importorskip("langgraph.checkpoint.postgres")
    from langmigrate.adapters.postgres import PostgresAdapter

    try:
        ad = PostgresAdapter.from_conn_string(PG)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")
    ad.setup()
    yield ad
    ad.close()


def _seed_legacy(saver, thread_id: str):
    from langgraph.checkpoint.base import empty_checkpoint

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hi"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    return saver.put(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})


def _raw_tuple(adapter, thread_id: str, chk_id: str | None = None):
    """Read a checkpoint straight from the saver (bypassing any interceptor)."""
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if chk_id is not None:
        cfg["configurable"]["checkpoint_id"] = chk_id
    return adapter.saver.get_tuple(cfg)


def test_batch_upgrade_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg = _seed_legacy(adapter.saver, thread_id)
    chk_id = cfg["configurable"]["checkpoint_id"]

    result = run_batch_upgrade(adapter, engine(), target="head")
    assert result.migrated >= 1

    tup = adapter.saver.get_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": chk_id}}
    )
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}
    assert tup.checkpoint["id"] == chk_id  # id preserved


def test_lazy_online_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)

    tup = interceptor.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert tup.checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"

    # Stored state is now current; it no longer shows up as stale.
    assert adapter.count_stale("v2") == 0


def test_batch_downgrade_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg = _seed_legacy(adapter.saver, thread_id)
    chk_id = cfg["configurable"]["checkpoint_id"]
    full = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": chk_id}}

    run_batch_upgrade(adapter, engine(), target="head")  # -> v2
    result = run_batch_downgrade(adapter, engine(), "v1")  # undo v2 (rename) only
    assert result.migrated >= 1

    tup = adapter.saver.get_tuple(full)
    assert tup.metadata[REVISION_METADATA_KEY] == "v1"
    # v2 rename reverted: back to `msgs`; context (from v1) still present.
    assert tup.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1, "context": {}}
    assert tup.checkpoint["id"] == chk_id

    # Restore to head so this thread does not linger as stale in the shared DB.
    run_batch_upgrade(adapter, engine(), target="head")


def test_list_does_not_write_back_e2e(adapter):
    # CLAUDE.md decision #3: list() migrates in memory for a consistent view but
    # must NEVER write back (it enumerates history — healing there is a write storm).
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)

    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    listed = list(interceptor.list(cfg))
    assert listed, "expected at least the seeded checkpoint"
    # The in-memory view IS migrated to head...
    assert listed[0].metadata[REVISION_METADATA_KEY] == "v2"
    assert listed[0].checkpoint["channel_values"] == {"messages": ["hi"], "count": 1, "context": {}}

    # ...but the stored row is untouched: still untagged and still legacy `msgs`.
    raw = _raw_tuple(adapter, thread_id)
    assert REVISION_METADATA_KEY not in (raw.metadata or {})
    assert raw.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    run_batch_upgrade(adapter, engine(), target="head")  # restore, don't leave stale


def test_online_dry_run_does_not_write_e2e(adapter):
    # `langmigrate upgrade --online-dry-run`: count stale checkpoints, write nothing.
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)

    result = run_batch_upgrade(adapter, engine(), target="head", dry_run=True)
    assert result.dry_run is True
    assert result.migrated >= 1  # the seeded checkpoint is counted as "would migrate"

    raw = _raw_tuple(adapter, thread_id)
    assert REVISION_METADATA_KEY not in (raw.metadata or {})
    assert raw.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    run_batch_upgrade(adapter, engine(), target="head")  # restore


def test_idempotent_reread_e2e(adapter):
    # Re-reading an already-migrated checkpoint is a no-op: same id, stable values,
    # and no second write-back (CLAUDE.md decision #3 idempotency).
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg = _seed_legacy(adapter.saver, thread_id)
    chk_id = cfg["configurable"]["checkpoint_id"]
    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)
    thread_cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

    first = interceptor.get_tuple(thread_cfg)
    second = interceptor.get_tuple(thread_cfg)
    assert first.checkpoint["id"] == chk_id
    assert second.checkpoint["id"] == chk_id  # id is stable across re-reads
    assert second.metadata[REVISION_METADATA_KEY] == "v2"
    assert second.checkpoint["channel_values"] == first.checkpoint["channel_values"]


def test_parent_chain_preserved_on_writeback_e2e(adapter):
    # A thread with TWO checkpoints (parent -> child). Lazy write-back of the child
    # must preserve both its id and its parent pointer (CLAUDE.md decision #3:
    # re-persisting must not break the parent_config chain).
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
    migrated = interceptor.get_tuple(cfg0)  # latest checkpoint = the child (id2)
    assert migrated.checkpoint["id"] == id2
    assert migrated.metadata[REVISION_METADATA_KEY] == "v2"
    # The child still points at its parent after write-back...
    assert migrated.parent_config is not None
    assert migrated.parent_config["configurable"]["checkpoint_id"] == id1
    # ...and the parent checkpoint is still retrievable on its own.
    parent = _raw_tuple(adapter, thread_id, id1)
    assert parent is not None
    assert parent.checkpoint["id"] == id1


def test_namespaced_checkpoint_migrated_e2e(adapter):
    # Subgraph checkpoints live under a non-empty checkpoint_ns. They must be
    # enumerated as stale and migrated, with the namespace preserved end to end.
    from langgraph.checkpoint.base import empty_checkpoint

    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    ns = "sub:graph"
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["x"], "count": 1}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    adapter.saver.put(cfg, chk, {"source": "loop"}, {"msgs": 1, "count": 1})

    stale_ns = [
        c["configurable"]["checkpoint_ns"]
        for c in adapter.iter_stale_configs("v2")
        if c["configurable"]["thread_id"] == thread_id
    ]
    assert stale_ns == [ns]  # enumerated with its namespace, not the empty default

    interceptor = MigrationInterceptor(adapter.saver, engine(), write_back=True)
    tup = interceptor.get_tuple(cfg)
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"
    assert tup.checkpoint["channel_values"] == {"messages": ["x"], "count": 1, "context": {}}
    assert tup.config["configurable"]["checkpoint_ns"] == ns


def test_untagged_in_revision_counts_e2e(adapter):
    # A checkpoint with no langmigrate_rev tag must surface as "<untagged>" in the
    # distribution (exercises the metadata->>'...' IS NULL / COALESCE path).
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    _seed_legacy(adapter.saver, thread_id)  # seeded without a revision tag

    counts = adapter.revision_counts()
    assert counts.get("<untagged>", 0) >= 1

    run_batch_upgrade(adapter, engine(), target="head")  # restore, don't leave stale


def test_stamp_and_revision_counts_e2e(adapter):
    thread_id = f"t-{uuid.uuid4().hex[:8]}"
    cfg = _seed_legacy(adapter.saver, thread_id)
    chk_id = cfg["configurable"]["checkpoint_id"]
    full = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": chk_id}}

    updated = adapter.stamp_all("v2")  # tag without migrating data
    assert updated >= 1

    tup = adapter.saver.get_tuple(full)
    assert tup.metadata[REVISION_METADATA_KEY] == "v2"
    # Data is NOT migrated by stamp — still legacy `msgs`.
    assert tup.checkpoint["channel_values"] == {"msgs": ["hi"], "count": 1}

    counts = adapter.revision_counts()
    assert counts.get("v2", 0) >= 1
