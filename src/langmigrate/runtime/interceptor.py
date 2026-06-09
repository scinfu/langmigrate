"""Lazy online migration: a checkpointer wrapper.

:class:`MigrationInterceptor` implements ``BaseCheckpointSaver`` and *delegates*
to a real saver (Postgres, Redis, in-memory, ...), staying database-agnostic. On
every load it upgrades the state through the engine's cascade; on every write it
stamps the current head revision into ``checkpoint.metadata``.

Write-back (on by default) re-persists a migrated checkpoint **idempotently**:
the checkpoint ``id`` and the ``parent_config`` chain are preserved, and only
channels whose value actually changed get a bumped version (so ``versions_seen``
stays valid for untouched channels).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from ..core.engine import HEAD, MigrationEngine
from ..core.version import envelope_from_parts, stamp_metadata
from .persistence import build_migrated_tuple, changed_versions, put_config


class MigrationInterceptor(BaseCheckpointSaver):
    """Wrap a checkpointer to migrate state lazily on load and tag it on write."""

    def __init__(
        self,
        saver: BaseCheckpointSaver,
        engine: MigrationEngine,
        *,
        write_back: bool = True,
        target: str = HEAD,
    ) -> None:
        self.saver = saver
        self.engine = engine
        self.write_back = write_back
        self.target = target
        # Reuse the wrapped saver's serializer so encode/decode stays consistent.
        super().__init__(serde=saver.serde)

    def get_next_version(self, current, channel=None):
        return self.saver.get_next_version(current, channel)

    # -- read path (lazy upgrade) ------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        tup = self.saver.get_tuple(config)
        if tup is None:
            return None
        migrated, changed = self._migrate_tuple(tup)
        if changed and self.write_back:
            self.saver.put(
                put_config(migrated),
                migrated.checkpoint,
                migrated.metadata,
                changed_versions(tup.checkpoint, migrated.checkpoint),
            )
        return migrated

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        tup = await self.saver.aget_tuple(config)
        if tup is None:
            return None
        migrated, changed = self._migrate_tuple(tup)
        if changed and self.write_back:
            await self.saver.aput(
                put_config(migrated),
                migrated.checkpoint,
                migrated.metadata,
                changed_versions(tup.checkpoint, migrated.checkpoint),
            )
        return migrated

    # -- write path (stamp revision) ---------------------------------------

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.saver.put(config, checkpoint, self._stamp(metadata), new_versions)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await self.saver.aput(config, checkpoint, self._stamp(metadata), new_versions)

    # -- pass-through -------------------------------------------------------

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.saver.put_writes(config, writes, task_id, task_path)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self.saver.aput_writes(config, writes, task_id, task_path)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # Migrates in memory for a consistent view, but deliberately does NOT
        # write back: list() enumerates history (often many checkpoints across
        # threads) and healing it here would cause a write storm and rewrite past
        # checkpoints. To persist migrations use get_tuple (lazy, on resume) or the
        # batch runner (langmigrate upgrade) — the proper "cure the DB" path.
        for tup in self.saver.list(config, filter=filter, before=before, limit=limit):
            yield self._migrate_tuple(tup)[0]

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        # Read-only migration, same rationale as list() above (no write-back).
        async for tup in self.saver.alist(config, filter=filter, before=before, limit=limit):
            yield self._migrate_tuple(tup)[0]

    # -- internals ----------------------------------------------------------

    def _stamp(self, metadata: CheckpointMetadata) -> CheckpointMetadata:
        """Tag outgoing metadata with the resolved head revision."""
        return stamp_metadata(dict(metadata or {}), self.engine.resolve_target(self.target))  # type: ignore[return-value]

    def _migrate_tuple(self, tup: CheckpointTuple) -> tuple[CheckpointTuple, bool]:
        """Return (possibly migrated tuple, whether anything changed)."""
        envelope = envelope_from_parts(tup.checkpoint["channel_values"], dict(tup.metadata or {}))
        migrated = self.engine.upgrade_state(envelope, self.target)
        if migrated is envelope:
            return tup, False
        return build_migrated_tuple(tup, migrated, self.saver), True
