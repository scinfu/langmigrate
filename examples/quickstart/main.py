"""The one-liner quickstart: ``setup_langmigrate`` + a ``@migration`` revision.

Shows the lowest-boilerplate path. Instead of wiring a ``MigrationRegistry``, a
``MigrationEngine`` and a ``MigrationInterceptor`` by hand, ``setup_langmigrate``
wraps any saver in a single call. The revision in ``migrations/`` is written with
the ``@migration`` decorator (no subclassing). Runs with no database:

    uv run python examples/quickstart/main.py

This file is type-checked with ``mypy --strict``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import REVISION_METADATA_KEY, setup_langmigrate

MIGRATIONS = Path(__file__).parent / "migrations"


def seed_legacy_thread(saver: InMemorySaver, thread_id: str) -> None:
    """Persist a v0-style checkpoint: untagged and missing the ``context`` field."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk: Checkpoint = empty_checkpoint()
    chk["channel_values"] = {"messages": ["hello"], "count": 3}
    chk["channel_versions"] = {"messages": 1, "count": 1}
    saver.put(config, chk, {"source": "loop"}, {"messages": 1, "count": 1})


def revision_of(metadata: Any) -> str | None:
    """Read the LangMigrate revision tag out of a checkpoint's metadata."""
    return dict(metadata or {}).get(REVISION_METADATA_KEY)


def main() -> None:
    saver = InMemorySaver()
    seed_legacy_thread(saver, "thread-1")

    # One line replaces MigrationRegistry + MigrationEngine + MigrationInterceptor.
    checkpointer = setup_langmigrate(saver, MIGRATIONS)

    config: RunnableConfig = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    migrated = checkpointer.get_tuple(config)
    assert migrated is not None  # noqa: S101 - demo invariant

    print("Loaded through setup_langmigrate (lazy upgrade to head):")
    print(f"  values   = {migrated.checkpoint['channel_values']}")
    print(f"  revision = {revision_of(migrated.metadata)!r}")


if __name__ == "__main__":
    main()
