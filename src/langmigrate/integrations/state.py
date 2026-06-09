"""State-level migration: apply the cascade to a plain state mapping.

For the managed-platform case the revision tag cannot live in ``checkpoint.metadata``
(we don't control the saver), so here it is carried as a reserved **state channel**
(default ``langmigrate_rev``). The owning graph must declare that channel — the
middleware shim does this for you via its ``state_schema``.

This helper is pure (no langchain / langgraph import) and returns only the state
*update* to merge, mirroring how LangGraph nodes/middleware report changes.

**Important limitation — channel removal.** LangGraph *merges* state updates, so a
node update cannot delete a channel. A migration that **renames** (``msgs`` ->
``messages``) or **drops** a field therefore cannot remove the old key at this
level: the new key is added, but the old one lingers. By default this is surfaced
via ``on_removed="warn"``. For migrations that must truly purge old channels, own
the checkpointer and use :class:`~langmigrate.runtime.interceptor.MigrationInterceptor`
(the saver path), which rebuilds ``channel_values`` wholesale and removes them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from ..core.engine import HEAD, MigrationEngine
from ..core.exceptions import ChannelRemovalUnsupportedError
from ..core.types import StateEnvelope

DEFAULT_STATE_REV_KEY = "langmigrate_rev"

OnRemoved = Literal["warn", "error", "ignore"]

logger = logging.getLogger("langmigrate.integrations.state")


def migrate_state_update(
    engine: MigrationEngine,
    state: Mapping[str, Any],
    *,
    target: str = HEAD,
    rev_key: str = DEFAULT_STATE_REV_KEY,
    on_removed: OnRemoved = "warn",
) -> dict[str, Any] | None:
    """Migrate ``state`` and return the update to merge, or ``None`` if up to date.

    The reserved ``rev_key`` is read from and written back into the state. Only
    added/changed channels are returned (plus the new tag).

    ``on_removed`` controls what happens when the migration removes channels
    (rename/drop) — which cannot be applied via a merged state update:

    - ``"warn"`` (default): log a warning and proceed (old key lingers).
    - ``"error"``: raise :class:`ChannelRemovalUnsupportedError`.
    - ``"ignore"``: proceed silently.
    """
    values = dict(state)
    current_rev = values.pop(rev_key, None)
    envelope = StateEnvelope(values=values, revision=current_rev)
    migrated = engine.upgrade_state(envelope, target)
    if migrated is envelope:
        return None

    removed = [key for key in values if key not in migrated.values]
    if removed:
        if on_removed == "error":
            raise ChannelRemovalUnsupportedError(removed)
        if on_removed == "warn":
            logger.warning(
                "langmigrate: state-level migration cannot remove channel(s) %s "
                "(LangGraph merges updates); the old key(s) will linger. Use "
                "MigrationInterceptor (the saver path) to purge them.",
                removed,
            )

    update: dict[str, Any] = {
        key: value
        for key, value in migrated.values.items()
        if key not in values or values[key] != value
    }
    update[rev_key] = migrated.revision
    return update
