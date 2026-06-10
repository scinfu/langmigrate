"""The adapter contract for proactive (batch) migration.

An adapter exposes a database's checkpoints to the batch CLI: it enumerates the
checkpoints whose stored revision is behind the target (ideally via an indexed
metadata query) and provides the underlying saver used to read/write them.

This module is pure — it declares a :class:`Protocol` only. Concrete adapters
(``postgres``, ``redis``) live alongside and import their DB client lazily.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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


@runtime_checkable
class AsyncCheckpointAdapter(Protocol):
    """Async counterpart of :class:`CheckpointAdapter` (for async savers)."""

    @property
    def saver(self) -> BaseCheckpointSaver:
        """The underlying (async-capable) LangGraph checkpointer."""
        ...

    def aiter_stale_configs(self, head: str) -> AsyncIterator[RunnableConfig]:
        """Yield a full ``RunnableConfig`` per stale checkpoint."""
        ...


@runtime_checkable
class AsyncBatchCheckpointAdapter(AsyncCheckpointAdapter, Protocol):
    """An :class:`AsyncCheckpointAdapter` that can enumerate *all* checkpoints."""

    def aiter_all_configs(self) -> AsyncIterator[RunnableConfig]:
        """Yield a full ``RunnableConfig`` for every checkpoint in the store."""
        ...


@runtime_checkable
class StoreAdapter(Protocol):
    """Backend-specific access to a LangGraph ``BaseStore`` for batch migration.

    Item references are ``(namespace, key)`` pairs. The revision tag lives under
    the reserved ``langmigrate_rev`` key *inside* each item's value.
    """

    @property
    def store(self):  # -> BaseStore (untyped to keep this Protocol import-light)
        """The underlying LangGraph store for reads/writes."""
        ...

    def iter_stale_items(self, head: str) -> Iterator[tuple[tuple[str, ...], str]]:
        """Yield ``(namespace, key)`` for every item whose tag differs from ``head``."""
        ...

    def iter_all_items(self) -> Iterator[tuple[tuple[str, ...], str]]:
        """Yield ``(namespace, key)`` for every item in the store."""
        ...

    def revision_counts(self) -> dict[str, int]:
        """Distribution of stored revision tags across all items."""
        ...

    def stamp_all(self, revision: str) -> int:
        """Set the revision tag on every item without running migrations."""
        ...
