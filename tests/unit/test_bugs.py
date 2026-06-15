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
    TopologyMismatchError,
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

    expr, todo = _coercion_expr("list[int]")
    # The expression itself must be a valid, comment-free callable; the TODO is
    # returned separately so it can be emitted on its own line.
    assert expr == "lambda v: v"
    assert "#" not in expr
    assert todo is not None and "TODO: implement manual coercion to list[int]" in todo

    builtin_expr, builtin_todo = _coercion_expr("int")
    assert builtin_expr == "int" and builtin_todo is None


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


# Bug #8: the fluent ``state.require_field(...)`` reported the *envelope's own*
# (source) revision in MissingRequiredFieldError instead of the migration that
# requires the field, diverging from ``self.require_field(state, ...)`` and
# pointing an operator at the wrong revision. The fluent helper has no handle
# on the migration being applied, so it now passes ``revision=None`` (no
# misleading value) while the method style keeps the accurate revision.


def test_require_field_fluent_does_not_report_source_revision():
    from langmigrate.core.exceptions import MissingRequiredFieldError

    class V1(BaseMigration):
        revision = "v1"
        down_revision = None

        def upgrade(self, state):
            return state

        def downgrade(self, state):
            return state

    # Fluent style: the migration that requires the field is v2.
    class V2Fluent(BaseMigration):
        revision = "v2"
        down_revision = "v1"

        def upgrade(self, state):
            return state.require_field("must")

        def downgrade(self, state):
            return state

    # Method style: same logical failure, accurate revision.
    class V2Method(BaseMigration):
        revision = "v2"
        down_revision = "v1"

        def upgrade(self, state):
            return self.require_field(state, "must")

        def downgrade(self, state):
            return state

    state = StateEnvelope(values={"other": 1}, revision="v1")

    eng_fluent = MigrationEngine(MigrationRegistry.from_migrations([V1(), V2Fluent()]))
    with pytest.raises(MissingRequiredFieldError) as fluent_exc:
        eng_fluent.upgrade_state(state, "v2")
    # The bug was reporting the *source* revision ("v1"); the fluent helper must
    # not claim a revision it cannot know.
    assert fluent_exc.value.revision is None
    assert "v1" not in str(fluent_exc.value)

    eng_method = MigrationEngine(MigrationRegistry.from_migrations([V1(), V2Method()]))
    with pytest.raises(MissingRequiredFieldError) as method_exc:
        eng_method.upgrade_state(state, "v2")
    # The method style still names the migration that requires the field.
    assert method_exc.value.revision == "v2"


# Bug #9: autogenerate produced an *unparseable* migration when a field's type
# changed to a non-builtin (e.g. ``list[int]``). The TODO placeholder was
# inlined as ``lambda v: v  # TODO ...`` right inside the ``coerce_field(...)``
# call, so the ``#`` commented out the closing ``)`` —
# ``SyntaxError: '(' was never closed`` — which broke loading the *whole*
# migrations directory. The TODO now goes on its own comment line and the
# expression stays a clean, valid ``lambda v: v``.


def test_autogenerate_non_builtin_coercion_renders_valid_python():
    import ast

    from langmigrate.core.schema import SchemaDiff, render_bodies

    diff = SchemaDiff(changed={"tags": ("str", "list[int]")})
    up, down = render_bodies(diff)

    for body in (up, down):
        # The coerce statement must parse on its own and must not have the TODO
        # swallowing the closing paren.
        rendered = "\n".join(body)
        assert "coerce_field" in rendered
        coerce_lines = [ln for ln in body if ln.startswith("state = self.coerce_field")]
        assert coerce_lines, body
        for line in coerce_lines:
            ast.parse(line)  # raises SyntaxError if the paren was swallowed
            assert line.rstrip().endswith(")")


def test_autogenerate_changed_type_migration_is_loadable(tmp_path):
    # End-to-end: a generated revision changing a field to a non-builtin type
    # must produce a file the registry can actually import.
    from langmigrate.cli.main import _create_revision

    schema_mod = tmp_path / "st.py"
    schema_mod.write_text(
        "from typing_extensions import TypedDict\nclass S(TypedDict):\n    tags: list[int]\n"
    )
    mig_dir = tmp_path / "migrations"
    sys.path.insert(0, str(tmp_path))
    try:
        _create_revision(mig_dir, "change tags", autogenerate=True, schema="st:S")
        reg = MigrationRegistry.from_path(mig_dir)  # must not raise SyntaxError
        assert len(reg) == 1
    finally:
        sys.path.remove(str(tmp_path))


# Bug #10: the batch runners (checkpoint + store) ignored the
# ``on_unknown_revision`` tolerance that the lazy paths honor. A single
# checkpoint/item tagged with a revision absent from the registry (the
# documented code-rollback-after-lazy-migration case) raised
# RevisionNotFoundError and aborted the WHOLE run — even though such state is
# simply ahead of the rolled-back code and should be left alone. The runners now
# accept ``on_unknown_revision`` ("raise" default keeps the old behavior;
# "warn"/"pass" skip the item, counting it in ``total`` but not ``migrated``).


def _stale_checkpoint_saver(stored_rev: str):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    cp = empty_checkpoint()
    cp["channel_values"] = {"foo": 1}
    cp["channel_versions"] = {"foo": "1"}
    saved = saver.put(
        cfg, cp, {"source": "input", "step": 0, REVISION_METADATA_KEY: stored_rev}, {"foo": "1"}
    )

    class _Adapter:
        def __init__(self) -> None:
            self.saver = saver

        def iter_stale_configs(self, head):
            yield {"configurable": dict(saved["configurable"])}

        def iter_all_configs(self):
            yield from self.iter_stale_configs("")

        def close(self) -> None:
            pass

    return _Adapter()


def test_batch_upgrade_raises_on_unknown_revision_by_default():
    from langmigrate.core.exceptions import RevisionNotFoundError
    from langmigrate.runtime.batch import run_batch_upgrade

    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    with pytest.raises(RevisionNotFoundError):
        run_batch_upgrade(_stale_checkpoint_saver("v99"), eng)


@pytest.mark.parametrize("policy", ["warn", "pass"])
def test_batch_upgrade_skips_unknown_revision_under_policy(policy):
    from langmigrate.runtime.batch import run_batch_upgrade

    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    result = run_batch_upgrade(_stale_checkpoint_saver("v99"), eng, on_unknown_revision=policy)
    # Enumerated (counted in total) but skipped, not migrated, and not a failure.
    assert result.total == 1
    assert result.migrated == 0
    assert result.ok


def test_batch_upgrade_warn_policy_does_not_swallow_other_revision_errors():
    # The tolerance applies ONLY to the checkpoint's OWN tag. A migration that
    # references a *different* unknown revision (a broken registry pointer / bad
    # target) must still surface, even under "warn"/"pass".
    from langmigrate.core.exceptions import RevisionNotFoundError
    from langmigrate.runtime.batch import run_batch_upgrade

    eng = MigrationEngine(MigrationRegistry.from_migrations([_SingleRevisionMigration()]))
    # Target a revision that does not exist: resolve_target raises up front.
    with pytest.raises(RevisionNotFoundError):
        run_batch_upgrade(_stale_checkpoint_saver("v1"), eng, target="nope")


def test_store_batch_upgrade_skips_unknown_revision_under_policy():
    from langmigrate.adapters.base import StoreAdapter
    from langmigrate.runtime.batch import run_store_batch_upgrade

    raw = InMemoryStore()
    # Stamp an item with an unknown revision directly inside its value.
    raw.put(("ns",), "k", {"msgs": ["hi"], REVISION_METADATA_KEY: "v99"})

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

    eng = MigrationEngine(MigrationRegistry.from_migrations([_RenameMsgsMigration()]))
    adapter = _AdHocAdapter(raw, [(("ns",), "k")])
    result = run_store_batch_upgrade(adapter, eng, on_unknown_revision="warn")
    assert result.total == 1 and result.migrated == 0 and result.ok
    # The unknown-tagged value was left untouched.
    assert raw.get(("ns",), "k").value[REVISION_METADATA_KEY] == "v99"


# Bug #11: NodeRemap redirected a renamed node to its target without checking the
# target exists in the current graph. A stale rename (pointing at a node that was
# itself removed) silently re-stranded the thread instead of surfacing a
# TopologyMismatchError. With known_nodes supplied, the target is now validated.


def test_noderemap_rename_to_missing_target_raises_when_known_nodes_given():
    from langmigrate.core.topology import NodeRemap

    remap = NodeRemap(renames={"old": "gone"})
    with pytest.raises(TopologyMismatchError) as ei:
        remap.resolve("old", known_nodes={"a", "b"})
    assert ei.value.node == "gone"


def test_noderemap_rename_to_valid_target_is_unaffected():
    from langmigrate.core.topology import NodeRemap

    remap = NodeRemap(renames={"old": "a"})
    assert remap.resolve("old", known_nodes={"a", "b"}) == "a"
    # Without known_nodes, the target is not validated (unchanged behavior).
    assert NodeRemap(renames={"old": "gone"}).resolve("old") == "gone"


# Bug #12 (SchemaMigrationMiddleware never forwarded on_reserved_key_collision)
# is covered in tests/unit/test_integrations_langchain.py, which stubs the
# optional ``langchain`` AgentMiddleware base.


# Bug #13: symmetric to Bug #11 — NodeRemap validated a rename *target* against
# known_nodes but not the *fallback*. A stale fallback (pointing at a node that
# was itself removed from the current graph) silently re-stranded the thread on a
# nonexistent node, the exact deadlock the rename validation guards against. With
# known_nodes supplied, the fallback is now validated too.


def test_noderemap_fallback_to_missing_node_raises_when_known_nodes_given():
    from langmigrate.core.topology import NodeRemap

    remap = NodeRemap(removed=["gone"], fallback="also_gone")
    with pytest.raises(TopologyMismatchError) as ei:
        remap.resolve("gone", known_nodes={"a", "b"})
    assert ei.value.node == "also_gone"


def test_noderemap_unknown_node_redirected_to_missing_fallback_raises():
    from langmigrate.core.topology import NodeRemap

    # A node not in known_nodes is treated as removed; the fallback it would be
    # redirected to is also absent from the current graph.
    remap = NodeRemap(fallback="gone")
    with pytest.raises(TopologyMismatchError) as ei:
        remap.resolve("ghost", known_nodes={"entry", "step"})
    assert ei.value.node == "gone"


def test_noderemap_valid_fallback_is_unaffected():
    from langmigrate.core.topology import NodeRemap

    remap = NodeRemap(removed=["gone"], fallback="entry")
    # Fallback exists in the current graph: redirected as before.
    assert remap.resolve("gone", known_nodes={"entry", "step"}) == "entry"
    # Without known_nodes, the fallback is not validated (unchanged behavior).
    assert NodeRemap(removed=["gone"], fallback="also_gone").resolve("gone") == "also_gone"


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
