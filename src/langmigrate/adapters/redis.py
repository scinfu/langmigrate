"""Redis adapter for proactive batch migration.

``langgraph-checkpoint-redis`` stores each checkpoint as a RedisJSON document at
``checkpoint:<thread>:<ns>:<id>`` with the LangGraph metadata kept as a serialized
JSON string under ``$.metadata``. Our revision tag is *not* part of the RediSearch
index, so batch enumeration scans the checkpoint keys and reads each document's
metadata (an O(n) sweep — inherent to Redis without a custom index).

Lazy *online* migration needs none of this: wrap a ``RedisSaver`` with
:class:`~langmigrate.runtime.interceptor.MigrationInterceptor` and it works today.
The ``redis`` client imports are done lazily so the rest of LangMigrate stays
importable without the ``[redis]`` extra.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata

from ..core.version import read_revision, stamp_metadata

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.checkpoint.redis import RedisSaver

# Matches checkpoint docs only ("checkpoint:..."), not "checkpoint_write:...".
_CHECKPOINT_MATCH = "checkpoint:*"


def _first(fields: dict[str, Any], path: str) -> Any:
    """First value at a RedisJSON ``$.path`` result (paths return a list)."""
    value = fields.get(path)
    return value[0] if value else None


class RedisAdapter:
    """Adapter over a ``RedisSaver`` for batch enumeration and stamping."""

    def __init__(self, saver: RedisSaver) -> None:
        self._saver = saver

    @classmethod
    def from_conn_string(cls, conn_string: str) -> RedisAdapter:
        """Open a connection and build the adapter (and its ``RedisSaver``)."""
        from langgraph.checkpoint.redis import RedisSaver

        saver = RedisSaver(conn_string)
        saver.setup()
        return cls(saver)

    @property
    def saver(self) -> RedisSaver:
        return self._saver

    def setup(self) -> None:
        self._saver.setup()

    # -- enumeration --------------------------------------------------------

    def _client(self) -> Any:
        return self._saver._redis

    def _iter_docs(self) -> Iterator[tuple[RunnableConfig, str | None]]:
        """Yield ``(config, stored_revision)`` for every checkpoint document."""
        from langgraph.checkpoint.redis.util import (
            from_storage_safe_id,
            from_storage_safe_str,
            safely_decode,
        )

        client = self._client()
        for raw_key in client.scan_iter(match=_CHECKPOINT_MATCH, count=200):
            key = safely_decode(raw_key)
            fields = client.json().get(
                key, "$.thread_id", "$.checkpoint_ns", "$.checkpoint_id", "$.metadata"
            )
            if not fields:
                continue

            thread_id = from_storage_safe_id(_first(fields, "$.thread_id") or "")
            checkpoint_ns = from_storage_safe_str(_first(fields, "$.checkpoint_ns") or "")
            checkpoint_id = from_storage_safe_id(_first(fields, "$.checkpoint_id") or "")
            revision = self._revision_from_metadata(_first(fields, "$.metadata"))
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            }
            yield config, revision

    @staticmethod
    def _revision_from_metadata(metadata: Any) -> str | None:
        # Stored as a serialized JSON string (occasionally already a dict).
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                return None
        return read_revision(metadata if isinstance(metadata, dict) else None)

    def count_stale(self, head: str) -> int:
        return sum(1 for _, rev in self._iter_docs() if rev != head)

    def iter_stale_configs(self, head: str) -> Iterator[RunnableConfig]:
        for config, rev in self._iter_docs():
            if rev != head:
                yield config

    def iter_all_configs(self) -> Iterator[RunnableConfig]:
        for config, _ in self._iter_docs():
            yield config

    def revision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, rev in self._iter_docs():
            key = rev or "<untagged>"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def stamp_all(self, revision: str) -> int:
        """Set the revision tag on every checkpoint without running migrations."""
        updated = 0
        for config, _ in self._iter_docs():
            tup = self._saver.get_tuple(config)
            if tup is None:
                continue
            metadata = stamp_metadata(dict(tup.metadata or {}), revision)
            put_config = tup.parent_config or {
                "configurable": {
                    "thread_id": config["configurable"]["thread_id"],
                    "checkpoint_ns": config["configurable"]["checkpoint_ns"],
                }
            }
            self._saver.put(put_config, tup.checkpoint, cast(CheckpointMetadata, metadata), {})
            updated += 1
        return updated

    def close(self) -> None:
        with contextlib.suppress(Exception):  # best effort
            self._client().close()

    def __enter__(self) -> RedisAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
