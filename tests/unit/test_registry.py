"""Unit tests for the migration registry and DAG resolution."""

from __future__ import annotations

import pytest

from langmigrate.core.exceptions import (
    CyclicHistoryError,
    DuplicateRevisionError,
    MultipleHeadsError,
    RevisionNotAncestorError,
    RevisionNotFoundError,
)
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.types import StateEnvelope


def mig(rev: str, down: str | None) -> BaseMigration:
    """Build a trivial migration with a given revision/down_revision."""

    class _M(BaseMigration):
        revision = rev
        down_revision = down
        slug = f"m_{rev}"

        def upgrade(self, state: StateEnvelope) -> StateEnvelope:
            return state

        def downgrade(self, state: StateEnvelope) -> StateEnvelope:
            return state

    return _M()


def linear_registry() -> MigrationRegistry:
    # base(v0) <- v1 <- v2
    return MigrationRegistry.from_migrations([mig("v0", None), mig("v1", "v0"), mig("v2", "v1")])


def test_head_and_bases_linear():
    reg = linear_registry()
    assert reg.head() == "v2"
    assert reg.bases() == ["v0"]


def test_lineage_order():
    assert linear_registry().lineage("v2") == ["v0", "v1", "v2"]


def test_upgrade_path_from_none_applies_all():
    assert linear_registry().upgrade_path(None, "v2") == ["v0", "v1", "v2"]


def test_upgrade_path_from_intermediate():
    assert linear_registry().upgrade_path("v0", "v2") == ["v1", "v2"]


def test_upgrade_path_already_at_target_is_empty():
    assert linear_registry().upgrade_path("v2", "v2") == []


def test_upgrade_path_unknown_from_raises():
    with pytest.raises(RevisionNotFoundError):
        linear_registry().upgrade_path("nope", "v2")


def test_downgrade_path_reverses():
    assert linear_registry().downgrade_path("v2", "v0") == ["v2", "v1"]


def test_downgrade_path_to_none_goes_past_base():
    assert linear_registry().downgrade_path("v2", None) == ["v2", "v1", "v0"]


def test_duplicate_revision_raises():
    with pytest.raises(DuplicateRevisionError):
        MigrationRegistry.from_migrations([mig("v0", None), mig("v0", None)])


def test_unknown_parent_raises():
    with pytest.raises(RevisionNotFoundError):
        MigrationRegistry.from_migrations([mig("v1", "ghost")])


def test_multiple_heads_detected():
    # v0 <- v1a and v0 <- v1b : two heads
    reg = MigrationRegistry.from_migrations([mig("v0", None), mig("v1a", "v0"), mig("v1b", "v0")])
    assert set(reg.heads()) == {"v1a", "v1b"}
    with pytest.raises(MultipleHeadsError):
        reg.head()


def test_empty_revision_id_rejected():
    with pytest.raises(ValueError, match="empty revision"):
        MigrationRegistry.from_migrations([mig("", None)])


def test_empty_registry_head_reports_no_revisions():
    # An empty registry has no head. The error must say so clearly rather than
    # claim "multiple heads: " with an empty list.
    reg = MigrationRegistry.from_migrations([])
    assert reg.heads() == []
    with pytest.raises(MultipleHeadsError) as exc:
        reg.head()
    assert "no revisions" in str(exc.value)
    assert "multiple heads" not in str(exc.value)


def test_cycle_detected():
    # a <- b and b <- a : both exist, but the lineage forms a cycle (no base)
    with pytest.raises(CyclicHistoryError):
        MigrationRegistry.from_migrations([mig("a", "b"), mig("b", "a")])


def test_get_unknown_raises():
    with pytest.raises(RevisionNotFoundError):
        linear_registry().get("ghost")


def test_downgrade_to_higher_revision_is_not_ancestor():
    # At v1, asking to downgrade to v2 (which sits ABOVE v1) is a clear, distinct error
    # from "revision unknown".
    with pytest.raises(RevisionNotAncestorError) as ei:
        linear_registry().downgrade_path("v1", "v2")
    assert ei.value.revision == "v2"
    assert ei.value.other == "v1"
    assert "not an ancestor" in str(ei.value)


def test_downgrade_to_unknown_revision_still_not_found():
    with pytest.raises(RevisionNotFoundError):
        linear_registry().downgrade_path("v2", "ghost")


def test_upgrade_from_non_ancestor_is_not_ancestor():
    # v1a and v1b are siblings; upgrading from v1a to v1b is not a valid path.
    reg = MigrationRegistry.from_migrations([mig("v0", None), mig("v1a", "v0"), mig("v1b", "v0")])
    with pytest.raises(RevisionNotAncestorError):
        reg.upgrade_path("v1a", "v1b")


def test_exceptions_exported_from_top_level():
    import langmigrate

    assert hasattr(langmigrate, "RevisionNotAncestorError")
    assert hasattr(langmigrate, "ChannelRemovalUnsupportedError")


# -- merge revisions (multi-parent DAG) ---------------------------------------


def _mk(revision: str, down_revision, field: str | None = None) -> BaseMigration:
    """Tiny migration adding `field` (or a no-op for merge points)."""
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


def diamond() -> MigrationRegistry:
    """base -> a, base -> b, merge(a, b)."""
    return MigrationRegistry.from_migrations(
        [
            _mk("base", None, "base_field"),
            _mk("a", "base", "a_field"),
            _mk("b", "base", "b_field"),
            _mk("merge", ("a", "b")),
        ]
    )


def test_parents_normalization():
    assert _mk("x", None).parents == ()
    assert _mk("x", "p").parents == ("p",)
    m = _mk("x", ("p", "q"))
    assert m.parents == ("p", "q")
    assert m.is_merge


def test_diamond_single_head():
    reg = diamond()
    assert reg.heads() == ["merge"]
    assert reg.head() == "merge"
    assert reg.bases() == ["base"]


def test_diamond_ancestors():
    reg = diamond()
    assert reg.ancestors("merge") == {"base", "a", "b"}
    assert reg.ancestors("a") == {"base"}
    assert reg.ancestors("base") == frozenset()


def test_diamond_upgrade_path_from_none_is_deterministic_topo():
    reg = diamond()
    # 'a' < 'b' lexicographically, so the min-heap tie-break fixes the order.
    assert reg.upgrade_path(None, "merge") == ["base", "a", "b", "merge"]


def test_diamond_upgrade_path_from_branch():
    reg = diamond()
    # From 'a': base and a are already applied; remaining diff is {b, merge}.
    assert reg.upgrade_path("a", "merge") == ["b", "merge"]


def test_diamond_upgrade_between_branches_raises():
    reg = diamond()
    with pytest.raises(RevisionNotAncestorError):
        reg.upgrade_path("a", "b")


def test_diamond_downgrade_paths():
    reg = diamond()
    assert reg.downgrade_path("merge", None) == ["merge", "b", "a", "base"]
    # Down to 'a': undo merge and b only (a and base stay applied).
    assert reg.downgrade_path("merge", "a") == ["merge", "b"]


def test_lineage_on_diamond_is_topological():
    reg = diamond()
    assert reg.lineage("merge") == ["base", "a", "b", "merge"]
    # Linear sub-lineage unchanged.
    assert reg.lineage("a") == ["base", "a"]


def test_merge_with_unknown_parent_raises():
    with pytest.raises(RevisionNotFoundError):
        MigrationRegistry.from_migrations([_mk("base", None), _mk("m", ("base", "ghost"))])


def test_merge_with_duplicate_parent_rejected():
    with pytest.raises(ValueError, match="duplicate parent"):
        MigrationRegistry.from_migrations([_mk("base", None), _mk("m", ("base", "base"))])


def test_cycle_through_tuple_parents_detected():
    with pytest.raises(CyclicHistoryError):
        MigrationRegistry.from_migrations([_mk("x", ("y",)), _mk("y", ("x",))])
