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
from ..core.exceptions import ChannelRemovalUnsupportedError, RevisionNotFoundError
from ..core.operations import strict_equal
from ..core.types import OnUnknownRevision, StateEnvelope

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
    on_unknown_revision: OnUnknownRevision = "raise",
) -> dict[str, Any] | None:
    """Migrate ``state`` and return the update to merge, or ``None`` if up to date.

    The reserved ``rev_key`` is read from and written back into the state. Only
    added/changed channels are returned (plus the new tag). A non-string tag is
    treated as untagged, mirroring how the checkpoint path reads metadata.

    ``on_removed`` controls what happens when the migration removes channels
    (rename/drop) — which cannot be applied via a merged state update:

    - ``"warn"`` (default): log a warning and proceed (old key lingers).
    - ``"error"``: raise :class:`ChannelRemovalUnsupportedError`.
    - ``"ignore"``: proceed silently.

    ``on_unknown_revision`` governs a tag the registry does not know (typically a
    code rollback after a lazy migration): ``"raise"`` (default) fails,
    ``"warn"``/``"pass"`` leave the state unmigrated (returning ``None``). As in
    the interceptor, the tolerance applies only to the state's own tag — a bad
    ``target`` still raises.

    With an empty registry (no revisions yet) there is nothing to migrate to, so
    the state is left untouched and ``None`` is returned.
    """
    if not len(engine.registry):
        return None
    values = dict(state)
    current_rev = values.pop(rev_key, None)
    if not isinstance(current_rev, str):
        current_rev = None
    envelope = StateEnvelope(values=values, revision=current_rev)
    try:
        migrated = engine.upgrade_state(envelope, target)
    except RevisionNotFoundError as exc:
        if on_unknown_revision == "raise" or exc.revision != envelope.revision:
            raise
        if on_unknown_revision == "warn":
            logger.warning(
                "langmigrate: state carries unknown revision %r (not in the registry); "
                "leaving it unmigrated. This usually means the code was rolled back "
                "after a lazy migration.",
                exc.revision,
            )
        return None
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

    # Strict (deep, type-aware) comparison: a coercion like 1 -> 1.0 compares ``==``
    # but is a real change; plain ``!=`` would drop it from the update while the
    # state is still stamped as migrated, silently losing the migration.
    update: dict[str, Any] = {
        key: value
        for key, value in migrated.values.items()
        if key not in values or not strict_equal(values[key], value)
    }
    update[rev_key] = migrated.revision
    return update
