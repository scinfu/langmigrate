"""Unit tests for the ``@migration`` decorator and the fluent StateEnvelope helpers."""

from __future__ import annotations

import pytest

from langmigrate import FunctionMigration, migration
from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import IrreversibleMigrationError
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import StateEnvelope


def _env(values, revision=None):
    return StateEnvelope(values=dict(values), revision=revision)


# -- fluent envelope helpers ------------------------------------------------


def test_envelope_add_and_drop_field():
    state = _env({"a": 1})
    added = state.add_field("b", factory=dict)
    assert added.values == {"a": 1, "b": {}}
    # original untouched (pure)
    assert state.values == {"a": 1}
    # idempotent: re-adding keeps the existing value
    assert added.add_field("b", default=99).values["b"] == {}
    assert added.drop_field("b").values == {"a": 1}


def test_envelope_rename_coerce_require():
    assert _env({"msgs": [1]}).rename_field("msgs", "messages").values == {"messages": [1]}
    assert _env({"n": "3"}).coerce_field("n", int).values == {"n": 3}
    assert _env({}).require_field("x", fallback=0).values == {"x": 0}


# -- the decorator ----------------------------------------------------------


@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state):
    return state.add_field("context", factory=dict)


@add_context.reverse
def _add_context_down(state):
    return state.drop_field("context")


def test_decorator_builds_function_migration():
    assert isinstance(add_context, FunctionMigration)
    assert add_context.revision == "a1c0"
    assert add_context.down_revision is None
    assert add_context.slug == "add_context"
    assert add_context.is_reversible


def test_decorator_upgrade_and_downgrade():
    up = add_context.upgrade(_env({"x": 1}))
    assert up.values == {"x": 1, "context": {}}
    down = add_context.downgrade(up)
    assert down.values == {"x": 1}


def test_reverse_returns_function_unchanged():
    # The reverse-decorated function stays a normal callable (not shadowed).
    assert callable(_add_context_down)
    assert _add_context_down(_env({"context": {}})).values == {}


def test_irreversible_without_reverse():
    @migration("b2d1", down_revision="a1c0")
    def bump(state):
        return state.add_field("v", default=1)

    assert not bump.is_reversible
    with pytest.raises(IrreversibleMigrationError):
        bump.downgrade(_env({"v": 1}))


def test_slug_defaults_to_function_name():
    @migration("c3e2", down_revision="b2d1")
    def my_change(state):
        return state

    assert my_change.slug == "my_change"


def test_empty_revision_rejected():
    with pytest.raises(ValueError, match="non-empty revision"):

        @migration("")
        def _bad(state):
            return state


def test_function_migrations_compose_in_engine():
    @migration("r1", down_revision=None, slug="add_a")
    def add_a(state):
        return state.add_field("a", default=1)

    @add_a.reverse
    def _(state):
        return state.drop_field("a")

    @migration("r2", down_revision="r1", slug="rename_a")
    def rename_a(state):
        return state.rename_field("a", "alpha")

    @rename_a.reverse
    def _(state):  # noqa: F811 - intentional reuse of throwaway name
        return state.rename_field("alpha", "a")

    engine = MigrationEngine(MigrationRegistry.from_migrations([add_a, rename_a]))
    out = engine.upgrade_state(_env({}, revision=None))
    assert out.values == {"alpha": 1}
    assert out.revision == "r2"
    # full downgrade back to base
    back = engine.downgrade_state(out, None)
    assert back.values == {}
