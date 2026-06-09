"""The migration engine: coordinates the cascade of transformations.

Given a :class:`StateEnvelope` tagged with its current revision, the engine
resolves the path through the DAG to the target and folds the cascade

    S_target = Ψ_{vn<-vn-1}( ... Ψ_{v1<-v0}(S_db) ... )

stamping the envelope's revision as it progresses. Upgrading state already at the
target is a **no-op**, which keeps lazy write-back idempotent.
"""

from __future__ import annotations

from .exceptions import LangMigrateError
from .migration import BaseMigration
from .registry import MigrationRegistry
from .types import StateEnvelope

#: Sentinel target meaning "the single head of the DAG".
HEAD = "head"


class MigrationEngine:
    """Coordinates upgrades and downgrades over a :class:`MigrationRegistry`."""

    def __init__(self, registry: MigrationRegistry) -> None:
        self.registry = registry

    # -- target resolution --------------------------------------------------

    def resolve_target(self, target: str = HEAD) -> str:
        """Resolve ``"head"`` to the concrete head revision; else validate ``target``."""
        if target == HEAD:
            return self.registry.head()
        # Raises RevisionNotFoundError if unknown.
        return self.registry.get(target).revision

    def head(self) -> str:
        """The single head revision of the DAG."""
        return self.registry.head()

    # -- path ---------------------------------------------------------------

    def path(self, from_revision: str | None, to_revision: str) -> list[BaseMigration]:
        """Ordered migrations to apply to upgrade from ``from`` to ``to``."""
        path = self.registry.upgrade_path(from_revision, to_revision)
        return [self.registry.get(rev) for rev in path]

    # -- upgrade ------------------------------------------------------------

    def is_stale(self, state: StateEnvelope, target: str = HEAD) -> bool:
        """Whether ``state`` is behind ``target`` and needs upgrading."""
        return state.revision != self.resolve_target(target)

    def upgrade_state(self, state: StateEnvelope, target: str = HEAD) -> StateEnvelope:
        """Fold the upgrade cascade up to ``target``. No-op if already there."""
        target_rev = self.resolve_target(target)
        if state.revision == target_rev:
            return state
        current = state
        for rev in self.registry.upgrade_path(state.revision, target_rev):
            migration = self.registry.get(rev)
            current = migration.upgrade(current).with_revision(rev)
        return current

    # -- downgrade ----------------------------------------------------------

    def downgrade_state(self, state: StateEnvelope, target: str | None) -> StateEnvelope:
        """Fold the downgrade cascade down to ``target`` (``None`` = past the base)."""
        if state.revision is None:
            raise LangMigrateError("Cannot downgrade an untagged state (no current revision)")
        if target is not None:
            target = self.resolve_target(target)
        if state.revision == target:
            return state
        current = state
        for rev in self.registry.downgrade_path(state.revision, target):
            migration = self.registry.get(rev)
            current = migration.downgrade(current).with_revision(migration.down_revision)
        return current
