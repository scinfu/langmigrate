"""PostgreSQL adapter for proactive batch migration.

Finds stale checkpoints with a single indexed query against the ``metadata``
JSONB column — no need to deserialize every row to discover its revision, which
is what makes the batch path scale to large databases.

The ``psycopg`` / ``langgraph-checkpoint-postgres`` imports are done lazily so the
rest of LangMigrate stays importable without the ``[postgres]`` extra installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from ..core.types import REVISION_METADATA_KEY

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.checkpoint.postgres import PostgresSaver

# Stale = the stored revision tag differs from (or is missing relative to) the head.
_STALE_WHERE = f"metadata->>'{REVISION_METADATA_KEY}' IS DISTINCT FROM %s"


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
        """Create the checkpoint tables if they do not yet exist."""
        self._saver.setup()

    def count_stale(self, head: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS c FROM checkpoints WHERE {_STALE_WHERE}", (head,))
            return int(cur.fetchone()["c"])

    def iter_stale_configs(self, head: str) -> Iterator[RunnableConfig]:
        # Materialize first so the cursor is closed before the saver reuses the
        # connection during migration of each checkpoint.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id "
                f"FROM checkpoints WHERE {_STALE_WHERE} "
                "ORDER BY thread_id, checkpoint_ns, checkpoint_id",
                (head,),
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

    def iter_all_configs(self) -> Iterator[RunnableConfig]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints "
                "ORDER BY thread_id, checkpoint_ns, checkpoint_id"
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
