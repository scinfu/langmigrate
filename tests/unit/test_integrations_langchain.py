"""Tests for the langchain middleware shim.

Covers the lazy/optional import behavior, plus a smoke test of the actual hooks
against a stub ``AgentMiddleware`` base (so a missing/renamed hook is caught here
rather than at runtime).
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from langmigrate.core.engine import MigrationEngine
from langmigrate.core.migration import BaseMigration
from langmigrate.core.registry import MigrationRegistry


def test_module_imports_without_langchain():
    # Importing the module must not require langchain.
    mod = importlib.import_module("langmigrate.integrations.langchain")
    assert hasattr(mod, "__getattr__")


def test_unknown_attribute_raises_attributeerror():
    mod = importlib.import_module("langmigrate.integrations.langchain")
    with pytest.raises(AttributeError):
        _ = mod.DoesNotExist


def test_middleware_access_requires_langchain():
    mod = importlib.import_module("langmigrate.integrations.langchain")
    langchain_installed = importlib.util.find_spec("langchain") is not None
    if langchain_installed:
        assert mod.SchemaMigrationMiddleware is not None
    else:
        with pytest.raises(ImportError, match="langchain"):
            _ = mod.SchemaMigrationMiddleware


# --- smoke test of the actual hooks against a stub AgentMiddleware base ------


class _V1(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        return self.drop_field(state, "context")


class _V2(BaseMigration):
    revision = "v2"
    down_revision = "v1"

    def upgrade(self, state):
        return self.rename_field(state, "msgs", "messages")

    def downgrade(self, state):
        return self.rename_field(state, "messages", "msgs")


@pytest.fixture
def stub_langchain(monkeypatch):
    """Inject a stub ``langchain.agents.middleware.AgentMiddleware`` into sys.modules."""

    class AgentMiddleware:
        def __init__(self, *a, **k):
            pass

    langchain = types.ModuleType("langchain")
    agents = types.ModuleType("langchain.agents")
    middleware = types.ModuleType("langchain.agents.middleware")
    middleware.AgentMiddleware = AgentMiddleware
    monkeypatch.setitem(sys.modules, "langchain", langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware", middleware)
    # The class is cached in the module namespace on first access (PEP 562
    # __getattr__ no longer rebuilds it every time); drop any cached copy so it
    # rebuilds against this stub, and clean up afterwards so later tests rebuild
    # against whatever langchain is really installed.
    lcmod = importlib.import_module("langmigrate.integrations.langchain")
    lcmod.__dict__.pop("SchemaMigrationMiddleware", None)
    yield AgentMiddleware
    lcmod.__dict__.pop("SchemaMigrationMiddleware", None)


def _engine():
    return MigrationEngine(MigrationRegistry.from_migrations([_V1(), _V2()]))


def test_middleware_builds_and_all_hooks_migrate(stub_langchain):
    mod = importlib.import_module("langmigrate.integrations.langchain")
    cls = mod.SchemaMigrationMiddleware
    # All four hooks must exist (this is what would silently break at runtime).
    for hook in ("before_agent", "before_model", "abefore_agent", "abefore_model"):
        assert hasattr(cls, hook)

    mw = cls(_engine())
    legacy = {"msgs": ["hi"], "count": 1}
    update = mw.before_agent(dict(legacy))
    assert update["messages"] == ["hi"]
    assert update["context"] == {}
    assert update["langmigrate_rev"] == "v2"

    # Idempotent: a state already at head yields no update.
    current = {"messages": ["hi"], "count": 1, "context": {}, "langmigrate_rev": "v2"}
    assert mw.before_model(current) is None


async def test_middleware_async_hooks_migrate(stub_langchain):
    mod = importlib.import_module("langmigrate.integrations.langchain")
    mw = mod.SchemaMigrationMiddleware(_engine())
    update = await mw.abefore_agent({"msgs": ["hi"], "count": 1})
    assert update["messages"] == ["hi"]
    assert update["langmigrate_rev"] == "v2"


def test_middleware_class_identity_is_stable(stub_langchain):
    # Regression: __getattr__ used to rebuild the class on every access, so two
    # imports yielded different classes and isinstance checks failed silently.
    mod = importlib.import_module("langmigrate.integrations.langchain")
    first = mod.SchemaMigrationMiddleware
    second = mod.SchemaMigrationMiddleware
    assert first is second
    assert isinstance(first(_engine()), second)


def test_middleware_unknown_revision_policy_forwarded(stub_langchain):
    mod = importlib.import_module("langmigrate.integrations.langchain")
    rolled_back = {"messages": ["hi"], "langmigrate_rev": "v99"}

    from langmigrate.core.exceptions import RevisionNotFoundError

    strict = mod.SchemaMigrationMiddleware(_engine())
    with pytest.raises(RevisionNotFoundError):
        strict.before_agent(dict(rolled_back))

    tolerant = mod.SchemaMigrationMiddleware(_engine(), on_unknown_revision="pass")
    assert tolerant.before_agent(dict(rolled_back)) is None


def test_middleware_forwards_reserved_key_collision_policy(stub_langchain):
    # Regression: the middleware never forwarded on_reserved_key_collision to
    # migrate_state_update, so it was stuck on the "warn" default and "error"
    # could not be selected. A non-string value under the reserved rev_key with
    # the "error" policy must now raise.
    from langmigrate.core.exceptions import ReservedKeyCollisionError

    mod = importlib.import_module("langmigrate.integrations.langchain")
    mw = mod.SchemaMigrationMiddleware(_engine(), on_reserved_key_collision="error")
    with pytest.raises(ReservedKeyCollisionError):
        mw.before_model({"messages": ["hi"], "langmigrate_rev": 42})

    # The default policy ("warn") still proceeds (no raise).
    tolerant = mod.SchemaMigrationMiddleware(_engine())
    assert tolerant.before_model({"messages": ["hi"], "langmigrate_rev": 42}) is not None


def test_custom_rev_key_declares_matching_channel(stub_langchain):
    # The contributed state channel must follow rev_key, or LangGraph would
    # reject the update as targeting an undeclared channel.
    mod = importlib.import_module("langmigrate.integrations.langchain")
    mw = mod.SchemaMigrationMiddleware(_engine(), rev_key="__rev__")
    assert "__rev__" in mw.state_schema.__annotations__

    update = mw.before_agent({"msgs": ["hi"], "count": 1})
    assert update["__rev__"] == "v2"

    # The default key keeps the class-level schema untouched.
    default = mod.SchemaMigrationMiddleware(_engine())
    assert "langmigrate_rev" in default.state_schema.__annotations__
