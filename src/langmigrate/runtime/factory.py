"""One-liner wiring for the lazy online interceptor.

:func:`setup_langmigrate` collapses the usual three-step setup —
``MigrationRegistry.from_path`` -> :class:`MigrationEngine` ->
:class:`MigrationInterceptor` — into a single call, so wrapping a saver becomes::

    saver = setup_langmigrate(base_saver, "migrations")

It is the runtime counterpart to the CLI: same engine, same migrations directory.
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver

from ..core.engine import HEAD, MigrationEngine
from ..core.registry import MigrationRegistry
from .interceptor import MigrationInterceptor


def setup_langmigrate(
    saver: BaseCheckpointSaver,
    migrations: str | Path | MigrationEngine | MigrationRegistry,
    *,
    write_back: bool = True,
    target: str = HEAD,
) -> MigrationInterceptor:
    """Wrap ``saver`` in a fully wired :class:`MigrationInterceptor`.

    ``migrations`` may be:

    - a ``str`` / :class:`~pathlib.Path` to a migrations directory (discovered via
      :meth:`MigrationRegistry.from_path`),
    - a :class:`MigrationRegistry`, or
    - an already-built :class:`MigrationEngine`.

    ``write_back`` and ``target`` are forwarded to :class:`MigrationInterceptor`
    (lazy write-back on by default; ``target`` defaults to the DAG head).
    """
    engine = _resolve_engine(migrations)
    return MigrationInterceptor(saver, engine, write_back=write_back, target=target)


def _resolve_engine(
    migrations: str | Path | MigrationEngine | MigrationRegistry,
) -> MigrationEngine:
    if isinstance(migrations, MigrationEngine):
        return migrations
    if isinstance(migrations, MigrationRegistry):
        return MigrationEngine(migrations)
    return MigrationEngine(MigrationRegistry.from_path(migrations))
