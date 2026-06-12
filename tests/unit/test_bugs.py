"""Unit tests for miscellaneous bug fixes and regressions."""

from __future__ import annotations

import logging
import sys

import pytest
from langgraph.store.memory import InMemoryStore

from langmigrate.core import operations as ops
from langmigrate.core.engine import MigrationEngine
from langmigrate.core.exceptions import (
    CyclicHistoryError,
    InvalidMigrationGraphError,
    ReservedKeyCollisionError,
)
from langmigrate.core.migration import BaseMigration, FunctionMigration
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.schema import load_schema
from langmigrate.core.types import (
    REVISION_METADATA_KEY,
    StateEnvelope,
)
from langmigrate.core.version import envelope_from_item_parts, strip_value_tag
from langmigrate.integrations.state import DEFAULT_STATE_REV_KEY, migrate_state_update
from langmigrate.runtime.store import MigrationStore


def test_rename_field_redundancy():
    state = StateEnvelope(values={"a": 1}, revision="v0")
    # Renaming "a" to "a" should return the same object (idempotency at object level)
    # to avoid redundant write-backs in the interceptor.
    new_state = ops.rename_field(state, "a", "a")
    assert new_state is state


def test_coerce_field_redundancy():
    state = StateEnvelope(values={"a": 1}, revision="v0")
    # Coercing to same value should return the same object.
    new_state = ops.coerce_field(state, "a", int)
    assert new_state is state

    # Test redundancy with a new object that is equal (e.g. list copy)
    state2 = StateEnvelope(values={"a": [1, 2]}, revision="v0")
    new_state2 = ops.coerce_field(state2, "a", list)  # list([1, 2]) is a new object
    assert new_state2 is state2


def test_annotated_introspection():
    from typing import Annotated

    from typing_extensions import TypedDict

    from langmigrate.core.schema import introspect

    class State(TypedDict):
        a: Annotated[int, "metadata"]

    schema = introspect(State)
    assert schema == {"a": "Annotated[int, 'metadata']"}


def test_annotated_rendering():
    from typing import Annotated

    from langmigrate.core.schema import _type_name

    t = Annotated[int, "foo", 42]
    assert _type_name(t) == "Annotated[int, 'foo', 42]"


def test_annotated_callable_metadata_is_address_free():
    # LangGraph reducers are functions; repr(fn) embeds a memory address that
    # changes each process and would make persisted schema snapshots diff as
    # "changed" on every autogenerate. Render them by qualified name instead.
    from typing import Annotated

    from langmigrate.core.schema import _type_name

    def add_messages(a, b):
        return a + b

    rendered = _type_name(Annotated[list, add_messages])
    assert "0x" not in rendered
    assert (
        rendered
        == "Annotated[list, test_annotated_callable_metadata_is_address_free.<locals>.add_messages]"
    )  # noqa: E501


def test_load_schema_nested(tmp_path):
    mod_path = tmp_path / "mymod.py"
    mod_path.write_text("class Outer:\n    class Inner:\n        a: int = 1\n")
    sys.path.insert(0, str(tmp_path))
    try:
        # This should handle nested attribute
        schema = load_schema("mymod:Outer.Inner")
        assert schema == {"a": "int"}
    finally:
        sys.path.remove(str(tmp_path))


def test_discovery_duplicate_class(tmp_path):
    mig_path = tmp_path / "v1_test.py"
    mig_path.write_text("""
from langmigrate.core.migration import BaseMigration
class MyMig(BaseMigration):
    revision = "v1"
    def upgrade(self, state): return state
AliasMig = MyMig
""")
    # If the same class is present under two names, it might be instantiated twice
    # leading to DuplicateRevisionError.
    reg = MigrationRegistry.from_path(tmp_path)
    assert len(reg) == 1


def test_literal_rendering_with_quotes():
    from typing import Literal

    from langmigrate.core.schema import _type_name

    t = Literal["a", "b"]
    assert _type_name(t) == "Literal['a', 'b']"


def test_coercion_expr_placeholder():
    from langmigrate.core.schema import _coercion_expr

    expr = _coercion_expr("list[int]")
    assert "TODO: implement manual coercion to list[int]" in expr


def test_discovery_cross_file_imports(tmp_path):
    # Tests that if v2.py imports a migration from v1.py, it doesn't get
    # registered twice (which would raise DuplicateRevisionError).
    (tmp_path / "v1.py").write_text(
        "from langmigrate.core.migration import BaseMigration\n"
        "class M1(BaseMigration):\n"
        "    revision = 'v1'\n"
        "    def upgrade(self, s): return s\n"
    )
    (tmp_path / "v2.py").write_text(
        "from v1 import M1\n"
        "from langmigrate.core.migration import BaseMigration\n"
        "class M2(BaseMigration):\n"
        "    revision = 'v2'\n"
        "    down_revision = 'v1'\n"
        "    def upgrade(self, s): return s\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        reg = MigrationRegistry.from_path(tmp_path)
        assert len(reg) == 2
        assert "v1" in reg
        assert "v2" in reg
    finally:
        sys.path.remove(str(tmp_path))


def test_callable_rendering():
    from collections.abc import Callable

    from langmigrate.core.schema import _type_name

    assert _type_name(Callable[[int], str]) == "Callable[[int], str]"


def test_discovery_does_not_pollute_sys_modules(tmp_path):
    # Discovery exposes migration files under their bare stem so cross-file
    # imports resolve, but it must restore sys.modules afterwards rather than
    # leaving generic names ("v1", "models", ...) registered globally.
    (tmp_path / "v1.py").write_text(
        "from langmigrate.core.migration import BaseMigration\n"
        "class M1(BaseMigration):\n"
        "    revision = 'v1'\n"
        "    def upgrade(self, s): return s\n"
    )
    before = dict(sys.modules)
    MigrationRegistry.from_path(tmp_path)
    assert "v1" not in sys.modules
    assert set(sys.modules) - set(before) == set()


def test_discovery_no_stale_reuse_across_calls(tmp_path, tmp_path_factory):
    # Two different directories each holding a file with the same stem must not
    # contaminate one another: the second call must load fresh content, not the
    # cached module from the first.
    dir_a = tmp_path_factory.mktemp("a")
    dir_b = tmp_path_factory.mktemp("b")
    (dir_a / "v1.py").write_text(
        "from langmigrate.core.migration import BaseMigration\n"
        "class M(BaseMigration):\n"
        "    revision = 'rev_a'\n"
        "    def upgrade(self, s): return s\n"
    )
    (dir_b / "v1.py").write_text(
        "from langmigrate.core.migration import BaseMigration\n"
        "class M(BaseMigration):\n"
        "    revision = 'rev_b'\n"
        "    def upgrade(self, s): return s\n"
    )
    reg_a = MigrationRegistry.from_path(dir_a)
    reg_b = MigrationRegistry.from_path(dir_b)
    assert "rev_a" in reg_a and "rev_a" not in reg_b
    assert "rev_b" in reg_b and "rev_b" not in reg_a


def test_discovery_forward_import_without_syspath(tmp_path):
    # v1 sorts before v2 yet imports from it (a "forward" reference). Discovery
    # must resolve this without the caller having added the directory to sys.path,
    # and must leave sys.path untouched afterwards.
    (tmp_path / "v1.py").write_text(
        "from v2 import SHARED\n"
        "from langmigrate.core.migration import BaseMigration\n"
        "class M1(BaseMigration):\n"
        "    revision = 'v1'\n"
        "    down_revision = 'v2'\n"
        "    def upgrade(self, s): return s\n"
    )
    (tmp_path / "v2.py").write_text(
        "SHARED = 42\n"
        "from langmigrate.core.migration import BaseMigration\n"
        "class M2(BaseMigration):\n"
        "    revision = 'v2'\n"
        "    def upgrade(self, s): return s\n"
    )
    assert str(tmp_path) not in sys.path
    reg = MigrationRegistry.from_path(tmp_path)
    assert "v1" in reg and "v2" in reg
    assert str(tmp_path) not in sys.path  # restored, not leaked


def test_public_api_exports_are_importable():
    # Guards against listing a name in __all__ that isn't actually bound on the
    # package (or vice-versa) — e.g. an exception raised by the registry but never
    # re-exported, so callers can't catch it from the top-level package.
    import langmigrate

    for name in langmigrate.__all__:
        assert hasattr(langmigrate, name), f"{name} is in __all__ but not importable"

    # Every public exception in core.exceptions should be re-exported.
    from langmigrate.core import exceptions

    public_excs = {
        name
        for name, obj in vars(exceptions).items()
        if isinstance(obj, type) and issubclass(obj, exceptions.LangMigrateError)
    }
    missing = public_excs - set(langmigrate.__all__)
    assert not missing, f"exceptions missing from langmigrate.__all__: {sorted(missing)}"


def test_discovery_does_not_shadow_real_module(tmp_path):
    # A migration file named like an importable module (here a sibling helper)
    # must not be left in sys.modules nor clobber a pre-existing entry.
    sentinel = object()
    sys.modules["v1"] = sentinel  # pretend a real "v1" module already exists
    (tmp_path / "v1.py").write_text(
        "from langmigrate.core.migration import BaseMigration\n"
        "class M1(BaseMigration):\n"
        "    revision = 'v1'\n"
        "    def upgrade(self, s): return s\n"
    )
    try:
        reg = MigrationRegistry.from_path(tmp_path)
        assert "v1" in reg
        # The pre-existing module is restored, not left overwritten.
        assert sys.modules["v1"] is sentinel
    finally:
        sys.modules.pop("v1", None)


# -- regression tests for the v1.1.x functional-bug sweep --------------------


# Bug #1: envelope_from_item_parts(None, ...) crashed with TypeError because
# strip_value_tag did ``dict(None)``. LangGraph's own stores never return an
# Item with ``value=None`` (PutOp(value=None) means delete), but external or
# custom BaseStore implementations can; such items must be served back as
# ``None``, not crash or be silently coerced to ``{}``.


def test_strip_value_tag_handles_none():
    assert strip_value_tag(None) == {}


def test_envelope_from_none_value_yields_empty_envelope():
    env = envelope_from_item_parts(None, namespace=("a",), key="k")
    assert env.values == {}
    assert env.revision is None


def test_migration_store_handles_none_value_returned_by_raw_store():
    # Regression: when a raw store returns an Item with ``value=None`` (e.g.
    # an external store that allows it, or a custom subclass), the wrapper
    # must not crash on ``dict(None)`` and must serve the ``None`` back. We
    # build a tiny stub that overrides ``batch`` (the only method
    # MigrationStore routes through); overriding ``get`` would not be
    # exercised.
    from langgraph.store.base import GetOp, Item

    class _NoneStore:
        supports_ttl = False
        ttl_config = None

        def batch(self, ops):
            return [
                Item(
                    namespace=tuple(op.namespace),
                    key=op.key,
                    value=None,
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                if isinstance(op, GetOp)
                else None
                for op in ops
            ]

        async def abatch(self, ops):
            return self.batch(ops)

    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    store = MigrationStore(_NoneStore(), eng)
    item = store.get(("ns",), "k1")
    assert item is not None
    assert item.value is None  # preserved end-to-end


def test_run_store_batch_upgrade_skips_none_values():
    # Regression: batch upgrade must skip None-valued items, not crash. The
    # InMemoryStore rejects ``put(value=None)`` outright, so we wrap it in a
    # stub whose ``get`` returns a None-valued Item for ``k_none`` and
    # delegates to InMemoryStore otherwise.
    from langgraph.store.base import Item

    from langmigrate.adapters.base import StoreAdapter
    from langmigrate.runtime.batch import run_store_batch_upgrade

    class _MixedStore(InMemoryStore):
        def get(self, namespace, key, *, refresh_ttl=None):
            if key == "k_none":
                return Item(
                    namespace=tuple(namespace),
                    key=key,
                    value=None,
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            return super().get(namespace, key, refresh_ttl=refresh_ttl)

    class _AdHocAdapter(StoreAdapter):
        def __init__(self, store, items):
            self._store = store
            self._items = items

        @property
        def store(self):
            return self._store

        def iter_stale_items(self, head):
            yield from self._items

        def iter_all_items(self):
            yield from self._items

        def close(self):
            pass

    raw = _MixedStore()
    raw.put(("ns",), "k_dict", {"msgs": ["hi"]})
    eng = MigrationEngine(MigrationRegistry.from_migrations([_RenameMsgsMigration()]))
    adapter = _AdHocAdapter(raw, [(("ns",), "k_none"), (("ns",), "k_dict")])
    result = run_store_batch_upgrade(adapter, eng)
    # Only the dict item was migrated; the None item was skipped, not crashed on.
    assert result.migrated == 1
    assert raw.get(("ns",), "k_dict").value[REVISION_METADATA_KEY] == "v1"


# Note on a previously proposed fix: build_migrated_tuple deliberately does
# NOT touch ``versions_seen`` for bumped channels. ``versions_seen`` is keyed
# by *node* ID (``langgraph.checkpoint.base.Checkpoint.versions_seen``), not
# by channel name; LangGraph re-aligns ``versions_seen[INTERRUPT]`` on every
# resume from the current ``channel_versions`` (``pregel/_loop.py:935-939``).
# The interceptor's design decision — "versions_seen stays valid for untouched
# channels" — is therefore both sufficient and correct: untouched channels
# keep their entries, and touched channels are re-aligned by LangGraph.


# Bug #3: MigrationRegistry accepted a non-string ``revision`` (e.g. ``int``).
# A user copy-paste mistake would then silently produce a checkpoint that
# ``read_revision`` treats as untagged.


def test_registry_rejects_non_string_revision_int():
    class M(BaseMigration):
        revision = 42
        down_revision = None

        def upgrade(self, state):
            return state

    with pytest.raises(TypeError, match="non-string"):
        MigrationRegistry([M()])


def test_registry_rejects_non_string_revision_none():
    class M(BaseMigration):
        revision = None
        down_revision = None

        def upgrade(self, state):
            return state

    with pytest.raises(TypeError, match="non-string"):
        MigrationRegistry([M()])


def test_function_migration_rejects_non_string_revision():
    def f(s):
        return s

    with pytest.raises(TypeError, match="string revision"):
        FunctionMigration(f, revision=42)


# Bug #4: a merge revision whose parents are in ancestor/descendant relation
# (e.g. ``("a", "base")`` when ``base`` is an ancestor of ``a``) was accepted
# silently by the registry, even though the CLI's `langmigrate merge`
# rejected it. The cascade itself is *unchanged* by the redundant edge
# (topological sort + ancestor-set difference in upgrade_path ignore it);
# the check is a hygiene / consistency check, not a correctness fix.


def test_registry_rejects_redundant_merge_parents():
    a = _mk_bare("base", None)
    b = _mk_bare("a", "base")
    m = _mk_bare("merge", ("a", "base"))  # 'base' is an ancestor of 'a'

    with pytest.raises(InvalidMigrationGraphError, match="redundant"):
        MigrationRegistry([a, b, m])


def test_registry_accepts_clean_merge_parents():
    # Same diamond as test_parents_normalization, sanity check that the new
    # redundant-parent validator does not break clean merges.
    a = _mk_bare("base", None)
    b = _mk_bare("a", "base")
    c = _mk_bare("b", "base")
    m = _mk_bare("merge", ("a", "b"))
    reg = MigrationRegistry([a, b, c, m])
    assert reg.head() == "merge"


# Bug #5: MigrationStore / migrate_state_update overwrote a user field
# literally named ``langmigrate_rev`` on every put without warning. The new
# ``on_reserved_key_collision`` policy surfaces the collision.


def test_store_warns_on_reserved_key_collision(caplog):
    raw = InMemoryStore()
    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    store = MigrationStore(raw, eng)
    with caplog.at_level(logging.WARNING, logger="langmigrate.runtime"):
        store.put(("ns",), "k1", {REVISION_METADATA_KEY: "user-data", "foo": "bar"})
    assert any("reserved key" in rec.message for rec in caplog.records)


def test_store_raises_on_reserved_key_collision_error():
    raw = InMemoryStore()
    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    store = MigrationStore(raw, eng, on_reserved_key_collision="error")
    with pytest.raises(ReservedKeyCollisionError):
        store.put(("ns",), "k1", {REVISION_METADATA_KEY: "user-data", "foo": "bar"})


def test_state_update_warns_on_reserved_key_collision(caplog):
    eng = MigrationEngine(MigrationRegistry.from_migrations([_AddContextMigration()]))
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        migrate_state_update(eng, {DEFAULT_STATE_REV_KEY: 42, "foo": "bar"})
    assert any("reserved" in rec.message for rec in caplog.records)


def test_state_update_raises_on_reserved_key_collision_error():
    eng = MigrationEngine(MigrationRegistry.from_migrations([_AddContextMigration()]))
    with pytest.raises(ReservedKeyCollisionError):
        migrate_state_update(
            eng,
            {DEFAULT_STATE_REV_KEY: 42, "foo": "bar"},
            on_reserved_key_collision="error",
        )


def test_state_update_does_not_warn_on_legitimate_tag(caplog):
    # A real (string, known-revision) value at ``rev_key`` is the documented
    # contract; the collision check must not warn in that case.
    eng = MigrationEngine(MigrationRegistry.from_migrations([_AddContextMigration()]))
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        migrate_state_update(eng, {DEFAULT_STATE_REV_KEY: "v1", "foo": "bar"})
    assert not any("reserved" in rec.message for rec in caplog.records)


def test_state_update_does_not_warn_on_none_tag(caplog):
    # ``None`` at ``rev_key`` is what a declared-but-unset state channel holds
    # (TypedDict defaults) — it carries no user data, so the collision check
    # must stay silent and the state is treated as untagged.
    eng = MigrationEngine(MigrationRegistry.from_migrations([_AddContextMigration()]))
    with caplog.at_level(logging.WARNING, logger="langmigrate.integrations.state"):
        update = migrate_state_update(eng, {DEFAULT_STATE_REV_KEY: None, "foo": "bar"})
    assert not any("reserved" in rec.message for rec in caplog.records)
    assert update[DEFAULT_STATE_REV_KEY] == "v1"


# Bug #7: a self-loop (``down_revision`` pointing to ``self``) reported the
# cycle as ``[rev, rev]``, which is confusing to read and trips up log
# parsers that match the path. The new message is ``["rev (self-loop)"]``.


def test_self_loop_error_message_is_clear():
    class M(BaseMigration):
        revision = "self"
        down_revision = "self"

        def upgrade(self, state):
            return state

    with pytest.raises(CyclicHistoryError) as excinfo:
        MigrationRegistry([M()])
    assert str(excinfo.value) == "Cycle detected in migration history involving: self (self-loop)"
    assert excinfo.value.revisions == ["self (self-loop)"]


# -- shared helpers for the regression tests --------------------------------


class _SingleRevisionMigration(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "context", default={})

    def downgrade(self, state):
        return self.drop_field(state, "context")


class _RenameMsgsMigration(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.rename_field(state, "msgs", "messages")

    def downgrade(self, state):
        return self.rename_field(state, "messages", "msgs")


class _AddContextMigration(BaseMigration):
    revision = "v1"
    down_revision = None

    def upgrade(self, state):
        return self.add_field(state, "context", default={})


def _mk_bare(rev: str, down) -> BaseMigration:
    class M(BaseMigration):
        # Bind the parameters under different names — the class body shadows
        # the parameter name otherwise (e.g. ``revision = revision``).
        revision = rev
        down_revision = down

        def upgrade(self, state):
            return state

    return M()
