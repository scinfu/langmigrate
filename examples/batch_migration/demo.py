"""Proactive (offline) batch migration with run_batch_upgrade / run_batch_downgrade.

This pattern is for teams that want to **cure the database before a release** rather
than relying on lazy online migration. Typical workflow:

    1. Deploy the new code with migrations in place.
    2. Run ``langmigrate upgrade head`` (or this script's batch runner) off-peak.
    3. All threads are now at head — the online interceptor becomes a no-op.

The demo uses a custom ``InMemoryAdapter`` (wraps ``InMemorySaver``) so it runs
without Docker. The same pattern works with ``PostgresAdapter`` for production.

Run:
    uv run python examples/batch_migration/demo.py
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import (
    REVISION_METADATA_KEY,
    MigrationEngine,
    MigrationRegistry,
    run_batch_downgrade,
    run_batch_upgrade,
)

MIGRATIONS = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# InMemoryAdapter — a minimal BatchCheckpointAdapter over InMemorySaver
# ---------------------------------------------------------------------------


class InMemoryAdapter:
    """Thin adapter so InMemorySaver can be used with the batch runner.

    A real project would use ``PostgresAdapter`` or ``RedisAdapter`` here.
    The protocol requires: ``saver``, ``count_stale``, ``iter_stale_configs``,
    and ``iter_all_configs`` (the last one needed for downgrade).
    """

    def __init__(self, saver: InMemorySaver) -> None:
        self._saver = saver

    @property
    def saver(self) -> BaseCheckpointSaver:
        return self._saver

    def _all_configs(self) -> list[RunnableConfig]:
        """Return one config per latest checkpoint (one per thread)."""
        # InMemorySaver.list(None) enumerates all checkpoints across all threads.
        seen: dict[str, RunnableConfig] = {}
        for tup in self._saver.list(None):
            tid = (tup.config or {}).get("configurable", {}).get("thread_id", "")
            # Keep only the latest (first listed) per thread.
            if tid and tid not in seen:
                seen[tid] = tup.config  # type: ignore[assignment]
        return list(seen.values())

    def count_stale(self, head: str) -> int:
        count = 0
        for cfg in self._all_configs():
            tup = self._saver.get_tuple(cfg)
            if tup and (tup.metadata or {}).get(REVISION_METADATA_KEY) != head:
                count += 1
        return count

    def iter_stale_configs(self, head: str) -> Iterator[RunnableConfig]:
        for cfg in self._all_configs():
            tup = self._saver.get_tuple(cfg)
            if tup and (tup.metadata or {}).get(REVISION_METADATA_KEY) != head:
                yield cfg

    def iter_all_configs(self) -> Iterator[RunnableConfig]:
        yield from self._all_configs()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

THREAD_DATA = [
    # (thread_id, values, meta_revision)
    ("task-001", {"task": "Summarise Q1 report", "status": "Pending", "result": ""}, None),
    ("task-002", {"task": "Generate invoice", "status": "IN_PROGRESS", "result": ""}, None),
    (
        "task-003",
        {
            "task": "Send weekly digest",
            "status": "done",
            "result": "sent",
            "priority": "high",
            "tags": [],
        },
        "a1c0",
    ),
    (
        "task-004",
        {
            "task": "Archive old threads",
            "status": "pending",
            "result": "",
            "priority": "low",
            "tags": ["maintenance"],
        },
        "a1c0",
    ),
    (
        "task-005",
        {
            "task": "Rotate API keys",
            "status": "completed",
            "result": "done",
            "priority": "critical",
            "tags": [],
            "attempt": 1,
        },
        "b2d1",
    ),
]


def seed_threads(saver: InMemorySaver) -> None:
    for thread_id, values, rev in THREAD_DATA:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        chk = empty_checkpoint()
        chk["channel_values"] = values
        chk["channel_versions"] = dict.fromkeys(values, 1)
        meta: dict = {"source": "loop"}
        if rev:
            meta[REVISION_METADATA_KEY] = rev
        saver.put(config, chk, meta, dict.fromkeys(values, 1))


def show_threads(saver: InMemorySaver, label: str) -> None:
    print(f"\n{label}")
    print(f"{'Thread':<12} {'Revision':<12} {'Status':<14} Fields")
    print("─" * 70)
    for thread_id, _, _ in THREAD_DATA:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        tup = saver.get_tuple(config)
        if tup is None:
            continue
        rev = (tup.metadata or {}).get(REVISION_METADATA_KEY, "<untagged>")
        vals = tup.checkpoint["channel_values"]
        status = vals.get("status", "—")
        fields = list(vals.keys())
        print(f"{thread_id:<12} {rev:<12} {status:<14} {fields}")
    print()


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def main() -> None:
    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()
    adapter = InMemoryAdapter(saver)

    seed_threads(saver)
    head = engine.head()
    print(f"Head revision: {head!r}")
    chain = engine.registry.lineage(head)
    print(f"Migrations: {' → '.join(chain)}\n")

    show_threads(saver, "INITIAL STATE (before any migration)")

    # ------------------------------------------------------------------
    # Step 1: Dry run — count stale checkpoints, don't touch the DB
    # ------------------------------------------------------------------
    dry = run_batch_upgrade(adapter, engine, dry_run=True)
    print(f"DRY RUN: {dry}")
    print(f"  stale   : {dry.total}")
    print(f"  changed : {dry.migrated}  (0 writes — dry_run=True)\n")

    # Confirm nothing changed
    after_dry = adapter.count_stale(head)
    print(f"Stale count after dry run (should be unchanged): {after_dry}\n")

    # ------------------------------------------------------------------
    # Step 2: Actual batch upgrade to HEAD
    # ------------------------------------------------------------------
    result = run_batch_upgrade(adapter, engine)
    print(f"BATCH UPGRADE: {result}")
    print(f"  target   : {result.target!r}")
    print(f"  migrated : {result.migrated}/{result.total}\n")

    show_threads(saver, "AFTER BATCH UPGRADE TO HEAD")

    # Verify all threads are at head
    remaining_stale = adapter.count_stale(head)
    print(f"Remaining stale checkpoints: {remaining_stale}  (expected 0)\n")

    # ------------------------------------------------------------------
    # Step 3: Batch downgrade to a1c0 (partial rollback)
    # ------------------------------------------------------------------
    print("BATCH DOWNGRADE to 'a1c0' (roll back normalise_status / attempt)...")
    down = run_batch_downgrade(adapter, engine, target="a1c0")
    print(f"  {down}")
    print(f"  migrated: {down.migrated}/{down.total}\n")

    show_threads(saver, "AFTER BATCH DOWNGRADE TO a1c0")

    # ------------------------------------------------------------------
    # Step 4: Downgrade to base (remove all revisions)
    # ------------------------------------------------------------------
    print("BATCH DOWNGRADE to base (remove all migrations)...")
    base = run_batch_downgrade(adapter, engine, target=None)
    print(f"  {base}")
    show_threads(saver, "AFTER BATCH DOWNGRADE TO BASE")


if __name__ == "__main__":
    main()
