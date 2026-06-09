"""Unit tests for graph topology remapping of interrupted threads."""

from __future__ import annotations

import pytest

from langmigrate.core.exceptions import TopologyMismatchError
from langmigrate.core.migration import BaseMigration
from langmigrate.core.topology import NodeRemap
from langmigrate.core.types import StateEnvelope


def test_rename_redirects_node():
    remap = NodeRemap(renames={"old_node": "new_node"})
    assert remap.resolve("old_node") == "new_node"


def test_unchanged_node_passthrough():
    remap = NodeRemap(renames={"a": "b"})
    assert remap.resolve("c") == "c"


def test_removed_node_with_fallback():
    remap = NodeRemap(removed=["gone"], fallback="entry")
    assert remap.resolve("gone") == "entry"


def test_removed_node_without_fallback_blocks():
    remap = NodeRemap(removed=["gone"])
    with pytest.raises(TopologyMismatchError) as ei:
        remap.resolve("gone")
    assert ei.value.node == "gone"


def test_known_nodes_treats_unknown_as_removed():
    remap = NodeRemap(fallback="entry")
    assert remap.resolve("ghost", known_nodes={"entry", "step"}) == "entry"


def test_known_nodes_unknown_without_fallback_raises():
    remap = NodeRemap()
    with pytest.raises(TopologyMismatchError):
        remap.resolve("ghost", known_nodes={"entry"})


def test_apply_updates_envelope_node():
    remap = NodeRemap(renames={"old": "new"})
    state = StateEnvelope(values={}, node="old")
    out = remap.apply(state)
    assert out.node == "new"
    assert state.node == "old"  # original untouched


def test_apply_noop_when_node_none():
    remap = NodeRemap(renames={"old": "new"})
    state = StateEnvelope(values={}, node=None)
    assert remap.apply(state) is state


def test_apply_noop_when_node_unchanged():
    # A node that is neither renamed nor missing resolves to itself; apply() must
    # return the same object rather than a needless copy.
    remap = NodeRemap(renames={"old": "new"})
    state = StateEnvelope(values={}, node="still_here")
    assert remap.apply(state) is state


# --- BaseMigration.remap_node helper --------------------------------------


class _M(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return state

    def downgrade(self, state):
        return state


def test_remap_node_helper_rename():
    state = StateEnvelope(values={}, node="old")
    out = _M().remap_node(state, renames={"old": "new"})
    assert out.node == "new"


def test_remap_node_helper_removed_with_fallback():
    state = StateEnvelope(values={}, node="gone")
    out = _M().remap_node(state, removed=["gone"], fallback="entry")
    assert out.node == "entry"


def test_remap_node_helper_blocks_without_fallback():
    state = StateEnvelope(values={}, node="gone")
    with pytest.raises(TopologyMismatchError):
        _M().remap_node(state, removed=["gone"])


def test_remap_node_helper_noop_when_node_none():
    state = StateEnvelope(values={}, node=None)
    assert _M().remap_node(state, renames={"old": "new"}) is state
