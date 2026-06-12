"""One-liner wiring for the lazy online interceptors.

:func:`setup_langmigrate` collapses the usual three-step setup —
``MigrationRegistry.from_path`` -> :class:`MigrationEngine` ->
:class:`MigrationInterceptor` — into a single call, so wrapping a saver becomes::

    saver = setup_langmigrate(base_saver, "migrations")

:func:`setup_langmigrate_store` does the same for a ``BaseStore``::

    store = setup_langmigrate_store(base_store, "store_migrations")

They are the runtime counterpart to the CLI: same engine, same migrations
directories.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.checkpoint.base import BaseCheckpointSaver

from ..core.engine import HEAD, MigrationEngine
from ..core.registry import MigrationRegistry
from ..core.types import OnReservedKeyCollision
from .interceptor import MigrationInterceptor, OnUnknownRevision

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from langgraph.store.base import BaseStore

    from .store import MigrationStore


def setup_langmigrate(
    saver: BaseCheckpointSaver,
    migrations: str | Path | MigrationEngine | MigrationRegistry,
    *,
    write_back: bool = True,
    target: str = HEAD,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> MigrationInterceptor:
    """Wrap ``saver`` in a fully wired :class:`MigrationInterceptor`.

    ``migrations`` may be:

    - a ``str`` / :class:`~pathlib.Path` to a migrations directory (discovered via
      :meth:`MigrationRegistry.from_path`),
    - a :class:`MigrationRegistry`, or
    - an already-built :class:`MigrationEngine`.

    ``write_back``, ``target`` and ``on_unknown_revision`` are forwarded to
    :class:`MigrationInterceptor` (lazy write-back on by default; ``target``
    defaults to the DAG head; unknown stored revisions raise by default —
    use ``"warn"`` in production to survive code rollbacks).
    """
    engine = _resolve_engine(migrations)
    return MigrationInterceptor(
        saver,
        engine,
        write_back=write_back,
        target=target,
        on_unknown_revision=on_unknown_revision,
    )


def setup_langmigrate_store(
    store: BaseStore,
    migrations: str | Path | MigrationEngine | MigrationRegistry,
    *,
    write_back: bool = True,
    target: str = HEAD,
    on_unknown_revision: OnUnknownRevision = "raise",
    on_reserved_key_collision: OnReservedKeyCollision = "warn",
) -> MigrationStore:
    """Wrap ``store`` in a fully wired :class:`MigrationStore`.

    Accepts the same ``migrations`` forms as :func:`setup_langmigrate`. Store
    migrations normally live in their own directory (``store_migrations``) since
    item shapes evolve independently of checkpoint channel shapes.

    ``on_reserved_key_collision`` is the policy applied when a put carries a
    value under the reserved ``langmigrate_rev`` key (which would otherwise be
    silently overwritten): ``"warn"`` (default) logs and proceeds,
    ``"error"`` raises :class:`ReservedKeyCollisionError`.
    """
    from .store import MigrationStore

    engine = _resolve_engine(migrations)
    return MigrationStore(
        store,
        engine,
        write_back=write_back,
        target=target,
        on_unknown_revision=on_unknown_revision,
        on_reserved_key_collision=on_reserved_key_collision,
    )


def _resolve_engine(
    migrations: str | Path | MigrationEngine | MigrationRegistry,
) -> MigrationEngine:
    if isinstance(migrations, MigrationEngine):
        return migrations
    if isinstance(migrations, MigrationRegistry):
        return MigrationEngine(migrations)
    return MigrationEngine(MigrationRegistry.from_path(migrations))
