"""The adapter contract for proactive (batch) migration.

An adapter exposes a database's checkpoints to the batch CLI: it enumerates the
checkpoints whose stored revision is behind the target (ideally via an indexed
metadata query) and provides the underlying saver used to read/write them.

This module is pure — it declares a :class:`Protocol` only. Concrete adapters
(``postgres``, ``redis``) live alongside and import their DB client lazily.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver


@runtime_checkable
class CheckpointAdapter(Protocol):
    """Backend-specific access used by the batch migration runner."""

    @property
    def saver(self) -> BaseCheckpointSaver:
        """The underlying LangGraph checkpointer for reads/writes."""
        ...

    def count_stale(self, head: str) -> int:
        """Number of checkpoints whose stored revision differs from ``head``."""
        ...

    def iter_stale_configs(self, head: str) -> Iterator[RunnableConfig]:
        """Yield a full ``RunnableConfig`` (incl. ``checkpoint_id``) per stale checkpoint."""
        ...


@runtime_checkable
class BatchCheckpointAdapter(CheckpointAdapter, Protocol):
    """A :class:`CheckpointAdapter` that can also enumerate *all* checkpoints.

    Needed for downgrades, whose target sits below the current head (so the
    stale-only enumeration is insufficient).
    """

    def iter_all_configs(self) -> Iterator[RunnableConfig]:
        """Yield a full ``RunnableConfig`` for every checkpoint in the store."""
        ...
