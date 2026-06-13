"""Core data types shared across LangMigrate.

These types are deliberately backend-agnostic. A :class:`StateEnvelope` is the
normalized view of a checkpoint that migrations operate on, so migration logic
never touches a database client or a LangGraph ``Checkpoint`` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Key under which the schema revision tag is stored inside ``checkpoint.metadata``.
REVISION_METADATA_KEY = "langmigrate_rev"

#: Policy for state tagged with a revision the registry does not know (typically a
#: code rollback after a lazy migration): fail the read, log and serve the state
#: unmigrated, or serve it unmigrated silently.
OnUnknownRevision = Literal["raise", "warn", "pass"]

#: Policy when an application stores a value under the reserved
#: :data:`REVISION_METADATA_KEY` (e.g. a field literally named
#: ``langmigrate_rev``). LangMigrate reserves that key for its own tag — without
#: a check, the wrapper would silently overwrite the user's data on every put
#: (and strip it on every read). ``"warn"`` logs a warning and proceeds
#: (overwriting the user value); ``"error"`` raises an explicit error.
OnReservedKeyCollision = Literal["warn", "error"]

# Sentinel distinguishing "no literal default given" from ``default=None``. Defined
# here (rather than in ``operations``) so the fluent ``StateEnvelope`` helpers can
# reference it without importing ``operations`` (which would be a circular import).
_OPS_UNSET = object()


class RevisionMeta(BaseModel):
    """Identity of a single migration revision within the DAG."""

    revision: str
    down_revision: str | tuple[str, ...] | None = None
    slug: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    branch_labels: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class StateEnvelope(BaseModel):
    """Normalized, backend-agnostic view of a checkpoint for migration.

    ``values`` mirrors a checkpoint's ``channel_values`` (the user state). The
    ``revision`` is the tag read from ``checkpoint.metadata`` and tells the engine
    where in the DAG this state currently sits. ``node`` is the (optional) node a
    thread is paused on, used by topology migrations.
    """

    values: dict[str, Any]
    revision: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    node: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def with_values(self, values: dict[str, Any]) -> StateEnvelope:
        """Return a copy carrying ``values`` (functional update; original untouched)."""
        return self.model_copy(update={"values": values})

    def with_revision(self, revision: str | None) -> StateEnvelope:
        """Return a copy stamped with ``revision``."""
        return self.model_copy(update={"revision": revision})

    # -- fluent declarative helpers -----------------------------------------
    #
    # These mirror :mod:`langmigrate.core.operations` as methods so inline
    # (``@migration``) and subclass migrations can write ``state.add_field(...)``.
    # ``operations`` is imported lazily to avoid a circular import (it depends on
    # this module). The transforms stay pure and idempotent.

    def add_field(
        self,
        name: str,
        default: Any = _OPS_UNSET,
        *,
        factory: Callable[[], Any] | None = None,
    ) -> StateEnvelope:
        """Safe: add ``name`` with a default/factory if absent. Idempotent."""
        from . import operations as ops

        return ops.add_field(self, name, default, factory=factory)

    def drop_field(self, name: str) -> StateEnvelope:
        """Safe: remove ``name``. No-op if already absent."""
        from . import operations as ops

        return ops.drop_field(self, name)

    def rename_field(self, old: str, new: str) -> StateEnvelope:
        """Unsafe: remap key ``old`` -> ``new``. Idempotent."""
        from . import operations as ops

        return ops.rename_field(self, old, new)

    def coerce_field(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        skip_if: Callable[[Any], bool] | None = None,
    ) -> StateEnvelope:
        """Unsafe: convert the value of ``name`` via ``fn``. No-op if absent."""
        from . import operations as ops

        return ops.coerce_field(self, name, fn, skip_if=skip_if)

    def require_field(
        self,
        name: str,
        *,
        fallback: Any = _OPS_UNSET,
        factory: Callable[[], Any] | None = None,
    ) -> StateEnvelope:
        """Unsafe: assert ``name`` exists, else inject a fallback or block."""
        from . import operations as ops

        # ``self.revision`` here is the envelope's *current* (source) tag — the
        # revision the state is migrating *from*, not the migration that requires
        # the field. Reporting it in MissingRequiredFieldError would point an
        # operator at the wrong revision. The fluent helper has no handle on the
        # migration being applied, so it passes ``revision=None`` rather than a
        # misleading value; ``BaseMigration.require_field`` supplies the accurate
        # migration revision.
        return ops.require_field(self, name, fallback=fallback, factory=factory, revision=None)
