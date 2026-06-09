"""Unit tests for miscellaneous bug fixes and regressions."""

from __future__ import annotations

import sys

from langmigrate.core import operations as ops
from langmigrate.core.registry import MigrationRegistry
from langmigrate.core.schema import load_schema
from langmigrate.core.types import StateEnvelope


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
