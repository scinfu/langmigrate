"""Unit tests for state-level migration (the managed-platform integration path)."""

from __future__ import annotations

import logging

import pytest

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import ChannelRemovalUnsupportedError
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.integrations.state import DEFAULT_STATE_REV_KEY, migrate_state_update


class V1(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        return self.drop_field(state, "context")


class V2(BaseMigration):
    revision = "v2"
    down_revision = "v1"

    def upgrade(self, state):
        state = self.rename_field(state, "msgs", "messages")
        return self.coerce_field(state, "count", int, skip_if=lambda v: isinstance(v, int))

    def downgrade(self, state):
        state = self.rename_field(state, "messages", "msgs")
        return self.coerce_field(state, "count", str)


def engine() -> MigrationEngine:
    return MigrationEngine(MigrationRegistry.from_migrations([V1(), V2()]))


def test_update_from_untagged_state():
    state = {"msgs": ["hi"], "count": "3"}
    update = migrate_state_update(engine(), state, target="head")
    assert update is not None
    assert update[DEFAULT_STATE_REV_KEY] == "v2"
    assert update["messages"] == ["hi"]
    assert update["count"] == 3
    assert update["context"] == {}


def test_update_is_none_when_current():
    state = {
        "messages": ["hi"],
        "count": 3,
        "context": {},
        DEFAULT_STATE_REV_KEY: "v2",
    }
    assert migrate_state_update(engine(), state, target="head") is None


def test_update_only_contains_changes_plus_tag():
    # Already has context; only needs the v2 step.
    state = {"msgs": ["hi"], "count": 3, "context": {"x": 1}, DEFAULT_STATE_REV_KEY: "v1"}
    update = migrate_state_update(engine(), state, target="head")
    assert set(update) == {"messages", DEFAULT_STATE_REV_KEY}
    assert update["messages"] == ["hi"]
    assert update[DEFAULT_STATE_REV_KEY] == "v2"


def test_custom_rev_key():
    state = {"msgs": ["hi"], "count": "3", "__rev__": None}
    update = migrate_state_update(engine(), state, target="head", rev_key="__rev__")
    assert update["__rev__"] == "v2"


# --- channel removal limitation (rename/drop on the merge path) ------------


def test_rename_warns_and_old_key_lingers_after_merge(caplog):
    state = {"msgs": ["hi"], "count": "3"}
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        update = migrate_state_update(engine(), state, target="head")  # default warn

    # The update adds the new key but cannot express removal of the old one.
    assert "messages" in update
    assert "msgs" not in update
    assert any("cannot remove channel" in r.message for r in caplog.records)

    # Simulate LangGraph's merge: the stale `msgs` key lingers (the documented limit).
    merged = {**state, **update}
    assert merged["messages"] == ["hi"]
    assert "msgs" in merged  # <-- not purged at the state level


def test_on_removed_error_raises():
    state = {"msgs": ["hi"], "count": "3"}
    with pytest.raises(ChannelRemovalUnsupportedError) as ei:
        migrate_state_update(engine(), state, target="head", on_removed="error")
    assert "msgs" in ei.value.channels


def test_on_removed_ignore_is_silent(caplog):
    state = {"msgs": ["hi"], "count": "3"}
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        update = migrate_state_update(engine(), state, target="head", on_removed="ignore")
    assert update["messages"] == ["hi"]
    assert not caplog.records


def test_pure_addition_does_not_warn(caplog):
    # V1 only adds `context`; nothing removed -> no warning regardless of policy.
    only_v1 = MigrationEngine(MigrationRegistry.from_migrations([V1()]))
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        update = migrate_state_update(only_v1, {"count": 1}, target="head")
    assert update["context"] == {}
    assert not caplog.records
