"""Unit tests for the migration engine: cascade, idempotency, no-op at HEAD."""

from __future__ import annotations

import pytest

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import IrreversibleMigrationError, LangMigrateError
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import StateEnvelope


class V1(BaseMigration):
    revision = "v1"
    down_revision = None
    slug = "add_context"

    def upgrade(self, state):
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        return self.drop_field(state, "context")


class V2(BaseMigration):
    revision = "v2"
    down_revision = "v1"
    slug = "rename_msgs"

    def upgrade(self, state):
        return self.rename_field(state, "msgs", "messages")

    def downgrade(self, state):
        return self.rename_field(state, "messages", "msgs")


class V3(BaseMigration):
    revision = "v3"
    down_revision = "v2"
    slug = "coerce_count"

    def upgrade(self, state):
        return self.coerce_field(state, "count", int, skip_if=lambda v: isinstance(v, int))

    def downgrade(self, state):
        return self.coerce_field(state, "count", str)


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1(), V2(), V3()]))


def test_resolve_head():
    assert engine().resolve_target() == "v3"


def test_full_cascade_from_untagged():
    e = engine()
    state = StateEnvelope(values={"msgs": ["hi"], "count": "5"}, revision=None)
    out = e.upgrade_state(state, "head")
    assert out.revision == "v3"
    assert out.values == {"messages": ["hi"], "count": 5, "context": {}}


def test_cascade_from_intermediate():
    e = engine()
    # already at v1 (has context, still uses 'msgs')
    state = StateEnvelope(values={"msgs": ["hi"], "count": "5", "context": {}}, revision="v1")
    out = e.upgrade_state(state, "head")
    assert out.revision == "v3"
    assert out.values == {"messages": ["hi"], "count": 5, "context": {}}


def test_noop_when_at_head():
    e = engine()
    state = StateEnvelope(values={"messages": [], "count": 0, "context": {}}, revision="v3")
    out = e.upgrade_state(state, "head")
    assert out is state  # untouched


def test_idempotent_double_upgrade():
    e = engine()
    state = StateEnvelope(values={"msgs": ["hi"], "count": "5"}, revision=None)
    once = e.upgrade_state(state, "head")
    twice = e.upgrade_state(once, "head")
    assert twice.values == once.values
    assert twice.revision == "v3"


def test_upgrade_to_specific_target():
    e = engine()
    state = StateEnvelope(values={"msgs": ["hi"], "count": "5"}, revision=None)
    out = e.upgrade_state(state, "v2")
    assert out.revision == "v2"
    # v1 (add context) + v2 (rename) applied; v3 (coerce count) NOT yet -> count stays str
    assert out.values == {"messages": ["hi"], "count": "5", "context": {}}


def test_is_stale():
    e = engine()
    assert e.is_stale(StateEnvelope(values={}, revision="v1"))
    assert not e.is_stale(StateEnvelope(values={}, revision="v3"))


def test_downgrade_cascade():
    e = engine()
    state = StateEnvelope(values={"messages": ["hi"], "count": 5, "context": {}}, revision="v3")
    out = e.downgrade_state(state, "v1")
    assert out.revision == "v1"
    assert out.values == {"msgs": ["hi"], "count": "5", "context": {}}


def test_downgrade_untagged_raises():
    e = engine()
    with pytest.raises(LangMigrateError):
        e.downgrade_state(StateEnvelope(values={}, revision=None), "v1")


def test_irreversible_downgrade_raises():
    class Irr(BaseMigration):
        revision = "x1"
        down_revision = None

        def upgrade(self, state):
            return state

    e = MigrationEngine(MigrationRegistry.from_migrations([Irr()]))
    state = StateEnvelope(values={}, revision="x1")
    with pytest.raises(IrreversibleMigrationError):
        e.downgrade_state(state, None)


def test_path_returns_ordered_migrations():
    # engine.path() resolves the ordered list of migration objects to apply.
    e = engine()
    migrations = e.path("v1", "v3")
    assert [m.revision for m in migrations] == ["v2", "v3"]
    # From the base (untagged) it is the whole lineage.
    assert [m.revision for m in e.path(None, "v3")] == ["v1", "v2", "v3"]
    # Already at the target → empty path.
    assert e.path("v3", "v3") == []


def test_downgrade_noop_when_already_at_target():
    # Downgrading to the revision the state already carries is a no-op and returns
    # the same object (keeps batch downgrade idempotent).
    e = engine()
    state = StateEnvelope(values={"messages": ["hi"], "count": 5, "context": {}}, revision="v2")
    assert e.downgrade_state(state, "v2") is state


# -- merge revisions through the engine ---------------------------------------


def _mk_merge(revision: str, down_revision, field: str | None = None):
    from langmigrate.core.migration import BaseMigration

    rev, down, fld = revision, down_revision, field

    class M(BaseMigration):
        revision = rev
        down_revision = down
        slug = rev

        def upgrade(self, state):
            if fld is None:
                return state
            return self.add_field(state, fld, default=True)

        def downgrade(self, state):
            if fld is None:
                return state
            return self.drop_field(state, fld)

    return M()


def diamond_engine() -> MigrationEngine:
    return MigrationEngine(
        MigrationRegistry.from_migrations(
            [
                _mk_merge("base", None, "base_field"),
                _mk_merge("a", "base", "a_field"),
                _mk_merge("b", "base", "b_field"),
                _mk_merge("merge", ("a", "b")),
            ]
        )
    )


def test_upgrade_through_merge_applies_both_branches():
    eng = diamond_engine()
    state = StateEnvelope(values={}, revision=None)

    out = eng.upgrade_state(state, "head")

    assert out.revision == "merge"
    assert out.values == {"base_field": True, "a_field": True, "b_field": True}


def test_upgrade_from_branch_through_merge():
    eng = diamond_engine()
    state = StateEnvelope(values={"base_field": True, "a_field": True}, revision="a")

    out = eng.upgrade_state(state, "head")

    assert out.revision == "merge"
    assert out.values == {"base_field": True, "a_field": True, "b_field": True}


def test_downgrade_through_merge_to_branch():
    eng = diamond_engine()
    state = StateEnvelope(
        values={"base_field": True, "a_field": True, "b_field": True}, revision="merge"
    )

    out = eng.downgrade_state(state, "a")

    # merge and b undone; final revision stamped once at the end.
    assert out.revision == "a"
    assert out.values == {"base_field": True, "a_field": True}


def test_downgrade_through_merge_past_base():
    eng = diamond_engine()
    state = StateEnvelope(
        values={"base_field": True, "a_field": True, "b_field": True}, revision="merge"
    )

    out = eng.downgrade_state(state, None)

    assert out.revision is None
    assert out.values == {}


def test_upgrade_at_merge_head_is_noop():
    eng = diamond_engine()
    state = StateEnvelope(
        values={"base_field": True, "a_field": True, "b_field": True}, revision="merge"
    )
    assert eng.upgrade_state(state, "head") is state
