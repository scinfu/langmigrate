"""The :class:`BaseMigration` developers subclass to define a revision.

A migration declares its position in the DAG (``revision`` / ``down_revision``)
and implements ``upgrade`` / ``downgrade`` over a :class:`StateEnvelope`. The
declarative helpers delegate to :mod:`langmigrate.core.operations`, which keeps the
transforms pure and idempotent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, NoReturn

from . import operations as ops
from .exceptions import IrreversibleMigrationError
from .types import RevisionMeta, StateEnvelope


class BaseMigration(ABC):
    """Base class for a single schema revision.

    Subclasses set the class attributes ``revision`` and ``down_revision`` (usually
    filled in by the ``langmigrate revision`` template) and implement ``upgrade``.
    Implement ``downgrade`` too, or mark the migration irreversible by calling
    :meth:`raise_irreversible` from it.
    """

    #: Unique revision id (Alembic-style hash). Set by subclasses.
    revision: str = ""
    #: Parent revision id(s) in the DAG: ``None`` for the base revision, a single
    #: id for a linear revision, or a tuple of ids for a **merge revision**.
    down_revision: str | tuple[str, ...] | None = None
    #: Human-readable label, e.g. ``"add_context_field"``.
    slug: str = ""
    #: Optional branch labels for DAG branching/merging.
    branch_labels: tuple[str, ...] = ()
    #: Optional schema snapshot ``{field: type_repr}`` *after* this revision.
    #: Written by ``revision --autogenerate`` and used as the baseline for the next.
    fields: dict[str, str] | None = None

    @property
    def parents(self) -> tuple[str, ...]:
        """``down_revision`` normalized to a tuple (empty for a base revision)."""
        if self.down_revision is None:
            return ()
        if isinstance(self.down_revision, str):
            return (self.down_revision,)
        return tuple(self.down_revision)

    @property
    def is_merge(self) -> bool:
        """Whether this revision joins multiple parents (a merge revision)."""
        return len(self.parents) > 1

    @abstractmethod
    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        """Transform ``state`` from ``down_revision`` up to ``revision``."""

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        """Reverse :meth:`upgrade`. Override; defaults to irreversible."""
        self.raise_irreversible()

    @property
    def is_reversible(self) -> bool:
        """Whether this migration provides a real downgrade.

        ``False`` means :meth:`downgrade` was left at the base (irreversible)
        implementation. Used by ``langmigrate check`` to flag one-way migrations.
        """
        return type(self).downgrade is not BaseMigration.downgrade

    # -- metadata -----------------------------------------------------------

    @property
    def meta(self) -> RevisionMeta:
        """Identity of this revision as a :class:`RevisionMeta`."""
        return RevisionMeta(
            revision=self.revision,
            down_revision=self.down_revision,
            slug=self.slug,
            branch_labels=self.branch_labels,
        )

    def raise_irreversible(self) -> NoReturn:
        """Signal that this migration cannot be downgraded."""
        raise IrreversibleMigrationError(self.revision)

    # -- declarative helpers (delegate to core.operations) ------------------

    def add_field(
        self,
        state: StateEnvelope,
        name: str,
        default: Any = ops._UNSET,
        *,
        factory: Callable[[], Any] | None = None,
    ) -> StateEnvelope:
        """Safe: add ``name`` with a default if absent."""
        return ops.add_field(state, name, default, factory=factory)

    def drop_field(self, state: StateEnvelope, name: str) -> StateEnvelope:
        """Safe: remove ``name``."""
        return ops.drop_field(state, name)

    def rename_field(self, state: StateEnvelope, old: str, new: str) -> StateEnvelope:
        """Unsafe: remap key ``old`` -> ``new``."""
        return ops.rename_field(state, old, new)

    def coerce_field(
        self,
        state: StateEnvelope,
        name: str,
        fn: Callable[[Any], Any],
        *,
        skip_if: Callable[[Any], bool] | None = None,
    ) -> StateEnvelope:
        """Unsafe: convert the value of ``name`` via ``fn``."""
        return ops.coerce_field(state, name, fn, skip_if=skip_if)

    def require_field(
        self,
        state: StateEnvelope,
        name: str,
        *,
        fallback: Any = ops._UNSET,
        factory: Callable[[], Any] | None = None,
    ) -> StateEnvelope:
        """Unsafe: assert ``name`` exists, else inject fallback or block."""
        return ops.require_field(
            state, name, fallback=fallback, factory=factory, revision=self.revision
        )

    def remap_node(
        self,
        state: StateEnvelope,
        *,
        renames: dict[str, str] | None = None,
        removed: list[str] | None = None,
        fallback: str | None = None,
        known_nodes: list[str] | None = None,
    ) -> StateEnvelope:
        """Repair an interrupted thread paused on a renamed/removed node.

        Only acts when ``state.node`` is set. Note that the runtime does not yet
        populate ``state.node`` automatically — set it in your migration (from your
        own checkpoint inspection) before calling this. See :class:`NodeRemap`.
        """
        from .topology import NodeRemap

        return NodeRemap(renames, removed, fallback=fallback).apply(state, known_nodes=known_nodes)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Migration {self.revision} <- {self.down_revision} ({self.slug})>"


# Type of a bare transform function: ``(state) -> state``.
_Transform = Callable[[StateEnvelope], StateEnvelope]


class FunctionMigration(BaseMigration):
    """A migration backed by plain ``(state) -> state`` functions.

    Created by the :func:`migration` decorator so developers can write inline
    function-pair migrations without subclassing :class:`BaseMigration`. The
    upgrade function is supplied at decoration time; the (optional) downgrade is
    attached afterwards via :meth:`reverse`. With no downgrade the migration is
    irreversible (``downgrade`` raises and :attr:`is_reversible` is ``False``).
    """

    def __init__(
        self,
        upgrade_fn: _Transform,
        *,
        revision: str,
        down_revision: str | tuple[str, ...] | None = None,
        slug: str = "",
        branch_labels: tuple[str, ...] = (),
        fields: dict[str, str] | None = None,
    ) -> None:
        if not revision:
            raise ValueError("@migration requires a non-empty revision id")
        self.revision = revision
        self.down_revision = down_revision
        self.slug = slug or getattr(upgrade_fn, "__name__", "")
        self.branch_labels = branch_labels
        self.fields = fields
        self._upgrade_fn = upgrade_fn
        self._downgrade_fn: _Transform | None = None

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self._upgrade_fn(state)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        if self._downgrade_fn is None:
            self.raise_irreversible()
        return self._downgrade_fn(state)

    @property
    def is_reversible(self) -> bool:
        return self._downgrade_fn is not None

    def reverse(self, fn: _Transform) -> _Transform:
        """Register ``fn`` as the downgrade and return it unchanged (decorator).

        Usage::

            @migration("a1c0", down_revision=None, slug="add_context")
            def add_context(state):
                return state.add_field("context", factory=dict)

            @add_context.reverse
            def _(state):
                return state.drop_field("context")
        """
        self._downgrade_fn = fn
        return fn


def migration(
    revision: str,
    *,
    down_revision: str | tuple[str, ...] | None = None,
    slug: str = "",
    branch_labels: tuple[str, ...] = (),
    fields: dict[str, str] | None = None,
) -> Callable[[_Transform], FunctionMigration]:
    """Decorator turning an upgrade function into a :class:`FunctionMigration`.

    Lets developers define a revision without subclassing :class:`BaseMigration`::

        @migration("a1c0", down_revision=None, slug="add_context")
        def add_context(state):
            return state.add_field("context", factory=dict)

        @add_context.reverse
        def _(state):
            return state.drop_field("context")

    The resulting :class:`FunctionMigration` instance is what the registry
    discovers (``MigrationRegistry.from_path`` picks up instances as well as
    subclasses). Attach the reverse transform with :meth:`FunctionMigration.reverse`;
    omit it to declare the migration irreversible.
    """

    def decorate(upgrade_fn: _Transform) -> FunctionMigration:
        return FunctionMigration(
            upgrade_fn,
            revision=revision,
            down_revision=down_revision,
            slug=slug,
            branch_labels=branch_labels,
            fields=fields,
        )

    return decorate
