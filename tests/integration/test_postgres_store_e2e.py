"""End-to-end integration test for the Postgres store adapter + batch/online migration.

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
from langmigrate.runtime.batch import run_store_batch_upgrade
from langmigrate.runtime.store import MigrationStore

pytestmark = pytest.mark.integration

PG = os.environ.get(
    "LANGMIGRATE_TEST_PG",
    "postgresql://langmigrate:langmigrate@localhost:5442/langmigrate",
)


class S1(BaseMigration):
    revision = "s1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "kind", default="memory")

    def downgrade(self, state):
        return self.drop_field(state, "kind")


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([S1()]))


@pytest.fixture
def adapter():
    pytest.importorskip("psycopg")
    pytest.importorskip("langgraph.store.postgres")
    from langmigrate.adapters.postgres import PostgresStoreAdapter

    try:
        ad = PostgresStoreAdapter.from_conn_string(PG)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")
    ad.setup()
    yield ad
    ad.close()


def test_store_batch_upgrade_e2e(adapter):
    ns = ("memories", f"u-{uuid.uuid4().hex[:8]}")
    adapter.store.put(ns, "m1", {"text": "hi"})

    result = run_store_batch_upgrade(adapter, engine(), target="head")
    assert result.migrated >= 1

    item = adapter.store.get(ns, "m1")
    assert item.value[REVISION_METADATA_KEY] == "s1"
    assert item.value["kind"] == "memory"


def test_store_lazy_online_e2e(adapter):
    ns = ("memories", f"u-{uuid.uuid4().hex[:8]}")
    adapter.store.put(ns, "m1", {"text": "hi"})
    wrapped = MigrationStore(adapter.store, engine(), write_back=True)

    item = wrapped.get(ns, "m1")
    assert item.value == {"text": "hi", "kind": "memory"}
    assert REVISION_METADATA_KEY not in item.value

    # Healed in place and no longer enumerated as stale.
    raw = adapter.store.get(ns, "m1")
    assert raw.value[REVISION_METADATA_KEY] == "s1"
    stale = list(adapter.iter_stale_items("s1"))
    assert (ns, "m1") not in stale


def test_store_setup_creates_revision_index_e2e(adapter):
    with adapter._conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'ix_store_langmigrate_rev' AND tablename = 'store'"
        )
        assert cur.fetchone() is not None


def test_store_revision_counts_and_stamp_e2e(adapter):
    ns = ("memories", f"u-{uuid.uuid4().hex[:8]}")
    adapter.store.put(ns, "m1", {"text": "hi"})

    counts = adapter.revision_counts()
    assert counts.get("<untagged>", 0) >= 1

    updated = adapter.stamp_all("s1")
    assert updated >= 1
    raw = adapter.store.get(ns, "m1")
    assert raw.value[REVISION_METADATA_KEY] == "s1"
    # Data NOT migrated by stamp.
    assert "kind" not in raw.value
