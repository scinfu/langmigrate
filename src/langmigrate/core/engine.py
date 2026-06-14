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
        """Fold the upgrade cascade up to ``target``. No-op if already there.

        If the state already sits **at or beyond** ``target`` (``target`` is an
        ancestor of the state's revision), there is nothing to upgrade and the
        state is returned unchanged rather than raising. This keeps a pinned
        older ``target`` from crashing on a checkpoint already written ahead of
        it — e.g. a mixed-version deploy or a partial rollback, where some
        threads were lazily migrated past the target. Genuinely divergent
        revisions (neither ancestor nor descendant of ``target``) are still
        rejected by :meth:`MigrationRegistry.upgrade_path`.
        """
        target_rev = self.resolve_target(target)
        if state.revision == target_rev:
            return state
        if state.revision is not None and target_rev in self.registry.ancestors(state.revision):
            return state
        current = state
        for rev in self.registry.upgrade_path(state.revision, target_rev):
            migration = self.registry.get(rev)
            current = migration.upgrade(current).with_revision(rev)
        return current

    # -- downgrade ----------------------------------------------------------

    def downgrade_state(self, state: StateEnvelope, target: str | None) -> StateEnvelope:
        """Fold the downgrade cascade down to ``target`` (``None`` = past the base).

        The final ``target`` revision is stamped once at the end: per-step stamping
        is ill-defined while undoing a merge revision (which parent would the state
        be "on"?), and for linear histories the result is identical.
        """
        if state.revision is None:
            raise LangMigrateError("Cannot downgrade an untagged state (no current revision)")
        if target is not None:
            target = self.resolve_target(target)
        if state.revision == target:
            return state
        # NOTE: unlike ``upgrade_state``, downgrading to a target that sits
        # *above* the current revision is deliberately surfaced as an error
        # (RevisionNotAncestorError, via ``downgrade_path``): the ``langmigrate
        # downgrade`` command treats "downgrade to a higher revision" as a clear
        # user mistake rather than silently doing nothing.
        path = self.registry.downgrade_path(state.revision, target)
        if not path:  # pragma: no cover - defensive: unreachable once state.revision != target
            # ``downgrade_path`` only returns ``[]`` for target == from_revision,
            # which the equality check above already short-circuits.
            return state
        current = state
        for rev in path:
            current = self.registry.get(rev).downgrade(current)
        return current.with_revision(target)
