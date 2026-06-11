"""PostgreSQL adapter for proactive batch migration.

Finds stale checkpoints with a single indexed query against the ``metadata``
JSONB column — no need to deserialize every row to discover its revision, which
is what makes the batch path scale to large databases.

The ``psycopg`` / ``langgraph-checkpoint-postgres`` imports are done lazily so the
rest of LangMigrate stays importable without the ``[postgres]`` extra installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from ..core.types import REVISION_METADATA_KEY

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres import PostgresStore

# Stale = the stored revision tag differs from (or is missing relative to) the head.
_STALE_WHERE = f"metadata->>'{REVISION_METADATA_KEY}' IS DISTINCT FROM %s"

# Expression index backing the stale-checkpoint queries above.
_REV_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_checkpoints_langmigrate_rev "
    f"ON checkpoints ((metadata->>'{REVISION_METADATA_KEY}'))"
)

# Page size for keyset-paginated enumeration (module constant so tests can shrink it).
_PAGE_SIZE = 500


class PostgresAdapter:
    """Adapter over a ``PostgresSaver`` connection for batch enumeration."""

    def __init__(self, conn: Any, saver: PostgresSaver) -> None:
        self._conn = conn
        self._saver = saver

    @classmethod
    def from_conn_string(cls, conn_string: str) -> PostgresAdapter:
        """Open a connection and build the adapter (and its ``PostgresSaver``)."""
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        conn = psycopg.connect(conn_string, autocommit=True, row_factory=dict_row)
        return cls(conn, PostgresSaver(conn))

    @property
    def saver(self) -> PostgresSaver:
        return self._saver

    def setup(self) -> None:
        """Create the checkpoint tables (if missing) and the revision-tag index."""
        self._saver.setup()
        with self._conn.cursor() as cur:
            cur.execute(_REV_INDEX_SQL)

    def count_stale(self, head: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS c FROM checkpoints WHERE {_STALE_WHERE}", (head,))
            return int(cur.fetchone()["c"])

    def iter_stale_configs(self, head: str) -> Iterator[RunnableConfig]:
        yield from self._iter_configs(_STALE_WHERE, (head,))

    def iter_all_configs(self) -> Iterator[RunnableConfig]:
        yield from self._iter_configs(None, ())

    def _iter_configs(
        self, where: str | None, params: tuple[object, ...]
    ) -> Iterator[RunnableConfig]:
        # Keyset pagination: each page closes its cursor before yielding, so the
        # saver can reuse the connection while migrating each checkpoint, and we
        # never materialize the full result set. A row healed mid-iteration simply
        # stops matching the stale predicate; the keyset still advances correctly.
        last: tuple[str, str, str] | None = None
        while True:
            clauses = [where] if where else []
            page_params: list[object] = list(params)
            if last is not None:
                clauses.append("(thread_id, checkpoint_ns, checkpoint_id) > (%s, %s, %s)")
                page_params.extend(last)
            where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id "
                    f"FROM checkpoints {where_sql}"
                    "ORDER BY thread_id, checkpoint_ns, checkpoint_id LIMIT %s",
                    (*page_params, _PAGE_SIZE),
                )
                rows = cur.fetchall()
            for row in rows:
                yield {
                    "configurable": {
                        "thread_id": row["thread_id"],
                        "checkpoint_ns": row["checkpoint_ns"],
                        "checkpoint_id": row["checkpoint_id"],
                    }
                }
            if len(rows) < _PAGE_SIZE:
                return
            last = (rows[-1]["thread_id"], rows[-1]["checkpoint_ns"], rows[-1]["checkpoint_id"])

    def stamp_all(self, revision: str) -> int:
        """Set the revision tag on every checkpoint without running migrations.

        Returns the number of rows updated. Use when adopting LangMigrate on a
        database whose state already matches a known revision. ``COALESCE`` guards
        against a row whose ``metadata`` is SQL/JSON ``null`` (``jsonb_set`` of a
        null base returns null and would silently drop the tag).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE checkpoints SET metadata = "
                "jsonb_set(COALESCE(NULLIF(metadata, 'null'::jsonb), '{}'::jsonb), "
                f"'{{{REVISION_METADATA_KEY}}}', to_jsonb(%s::text))",
                (revision,),
            )
            return int(cur.rowcount or 0)

    def revision_counts(self) -> dict[str, int]:
        """Distribution of stored revision tags across all checkpoints (for ``current --db``)."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT metadata->>'{REVISION_METADATA_KEY}' AS rev, count(*) AS c "
                "FROM checkpoints GROUP BY rev"
            )
            return {(row["rev"] or "<untagged>"): int(row["c"]) for row in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PostgresAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncPostgresAdapter:
    """Async adapter over an ``AsyncPostgresSaver`` for batch enumeration.

    Mirrors :class:`PostgresAdapter` (same SQL constants, same keyset pagination)
    for applications that run the proactive batch inside an async service.
    """

    def __init__(self, conn: Any, saver: AsyncPostgresSaver) -> None:
        self._conn = conn
        self._saver = saver

    @classmethod
    async def from_conn_string(cls, conn_string: str) -> AsyncPostgresAdapter:
        """Open an async connection and build the adapter (and its saver)."""
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(
            conn_string, autocommit=True, row_factory=dict_row
        )
        return cls(conn, AsyncPostgresSaver(conn))

    @property
    def saver(self) -> AsyncPostgresSaver:
        return self._saver

    async def setup(self) -> None:
        """Create the checkpoint tables (if missing) and the revision-tag index."""
        await self._saver.setup()
        async with self._conn.cursor() as cur:
            await cur.execute(_REV_INDEX_SQL)

    async def aiter_stale_configs(self, head: str) -> AsyncIterator[RunnableConfig]:
        async for config in self._aiter_configs(_STALE_WHERE, (head,)):
            yield config

    async def aiter_all_configs(self) -> AsyncIterator[RunnableConfig]:
        async for config in self._aiter_configs(None, ()):
            yield config

    async def _aiter_configs(
        self, where: str | None, params: tuple[object, ...]
    ) -> AsyncIterator[RunnableConfig]:
        # Same keyset pagination as the sync adapter (see PostgresAdapter._iter_configs).
        last: tuple[str, str, str] | None = None
        while True:
            clauses = [where] if where else []
            page_params: list[object] = list(params)
            if last is not None:
                clauses.append("(thread_id, checkpoint_ns, checkpoint_id) > (%s, %s, %s)")
                page_params.extend(last)
            where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id "
                    f"FROM checkpoints {where_sql}"
                    "ORDER BY thread_id, checkpoint_ns, checkpoint_id LIMIT %s",
                    (*page_params, _PAGE_SIZE),
                )
                rows = await cur.fetchall()
            for row in rows:
                yield {
                    "configurable": {
                        "thread_id": row["thread_id"],
                        "checkpoint_ns": row["checkpoint_ns"],
                        "checkpoint_id": row["checkpoint_id"],
                    }
                }
            if len(rows) < _PAGE_SIZE:
                return
            last = (rows[-1]["thread_id"], rows[-1]["checkpoint_ns"], rows[-1]["checkpoint_id"])

    async def aclose(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> AsyncPostgresAdapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# The store keeps the tag INSIDE the jsonb `value` (items have no metadata column).
_STORE_STALE_WHERE = f"value->>'{REVISION_METADATA_KEY}' IS DISTINCT FROM %s"

_STORE_REV_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_store_langmigrate_rev "
    f"ON store ((value->>'{REVISION_METADATA_KEY}'))"
)


class PostgresStoreAdapter:
    """Adapter over a ``PostgresStore`` for batch enumeration and stamping.

    The ``store`` table keys items by ``(prefix, key)`` where ``prefix`` is the
    dot-joined namespace tuple.
    """

    def __init__(self, conn: Any, store: PostgresStore) -> None:
        self._conn = conn
        self._store = store

    @classmethod
    def from_conn_string(cls, conn_string: str) -> PostgresStoreAdapter:
        """Open a connection and build the adapter (and its ``PostgresStore``)."""
        import psycopg
        from langgraph.store.postgres import PostgresStore
        from psycopg.rows import dict_row

        conn = psycopg.connect(conn_string, autocommit=True, row_factory=dict_row)
        return cls(conn, PostgresStore(conn))

    @property
    def store(self) -> PostgresStore:
        return self._store

    def setup(self) -> None:
        """Create the store tables (if missing) and the revision-tag index."""
        self._store.setup()
        with self._conn.cursor() as cur:
            cur.execute(_STORE_REV_INDEX_SQL)

    def iter_stale_items(self, head: str) -> Iterator[tuple[tuple[str, ...], str]]:
        yield from self._iter_items(_STORE_STALE_WHERE, (head,))

    def iter_all_items(self) -> Iterator[tuple[tuple[str, ...], str]]:
        yield from self._iter_items(None, ())

    def _iter_items(
        self, where: str | None, params: tuple[object, ...]
    ) -> Iterator[tuple[tuple[str, ...], str]]:
        # Keyset pagination, mirroring the checkpoint adapters.
        last: tuple[str, str] | None = None
        while True:
            clauses = [where] if where else []
            page_params: list[object] = list(params)
            if last is not None:
                clauses.append("(prefix, key) > (%s, %s)")
                page_params.extend(last)
            where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT prefix, key FROM store {where_sql}ORDER BY prefix, key LIMIT %s",
                    (*page_params, _PAGE_SIZE),
                )
                rows = cur.fetchall()
            for row in rows:
                yield tuple(row["prefix"].split(".")), row["key"]
            if len(rows) < _PAGE_SIZE:
                return
            last = (rows[-1]["prefix"], rows[-1]["key"])

    def stamp_all(self, revision: str) -> int:
        """Set the revision tag inside every item's value without migrating.

        ``COALESCE`` guards against a row whose ``value`` is SQL/JSON ``null``
        (``jsonb_set`` of a null base returns null and would silently drop the
        tag), mirroring :meth:`PostgresAdapter.stamp_all`.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE store SET value = "
                "jsonb_set(COALESCE(NULLIF(value, 'null'::jsonb), '{}'::jsonb), "
                f"'{{{REVISION_METADATA_KEY}}}', to_jsonb(%s::text))",
                (revision,),
            )
            return int(cur.rowcount or 0)

    def revision_counts(self) -> dict[str, int]:
        """Distribution of stored revision tags across all items."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT value->>'{REVISION_METADATA_KEY}' AS rev, count(*) AS c "
                "FROM store GROUP BY rev"
            )
            return {(row["rev"] or "<untagged>"): int(row["c"]) for row in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PostgresStoreAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
