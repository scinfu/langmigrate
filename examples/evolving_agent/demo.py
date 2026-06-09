"""Runnable demo of LangMigrate's lazy online migration.

Simulates an old (v0) thread persisted before the schema evolved, then loads it
through a ``MigrationInterceptor`` and shows it arrive at the head revision with
the database self-healed via idempotent write-back. Uses ``InMemorySaver`` so it
runs with no database:

    uv run python examples/evolving_agent/demo.py
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import (
    REVISION_METADATA_KEY,
    MigrationEngine,
    MigrationInterceptor,
    MigrationRegistry,
)

MIGRATIONS = Path(__file__).parent / "migrations"


def seed_legacy_thread(saver: InMemorySaver, thread_id: str) -> None:
    """Persist a v0-style checkpoint: untagged, uses `msgs`, `count` is a string."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = {"msgs": ["hello"], "count": "3"}
    chk["channel_versions"] = {"msgs": 1, "count": 1}
    saver.put(config, chk, {"source": "loop"}, {"msgs": 1, "count": 1})


def main() -> None:
    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}

    seed_legacy_thread(saver, "thread-1")
    raw = saver.get_tuple(config)
    print("BEFORE (raw v0 state in DB):")
    print(f"  values   = {raw.checkpoint['channel_values']}")
    print(f"  revision = {raw.metadata.get(REVISION_METADATA_KEY)!r}")
    print(f"  head     = {engine.head()!r}\n")

    interceptor = MigrationInterceptor(saver, engine, write_back=True)
    migrated = interceptor.get_tuple(config)
    print("AFTER (loaded through the interceptor, cascade v0->a1c0->b2d1):")
    print(f"  values   = {migrated.checkpoint['channel_values']}")
    print(f"  revision = {migrated.metadata[REVISION_METADATA_KEY]!r}\n")

    healed = saver.get_tuple(config)
    print("DB is now self-healed (write-back), id preserved:")
    print(f"  values   = {healed.checkpoint['channel_values']}")
    print(f"  revision = {healed.metadata[REVISION_METADATA_KEY]!r}")
    print(f"  same id  = {healed.checkpoint['id'] == raw.checkpoint['id']}")


if __name__ == "__main__":
    main()
