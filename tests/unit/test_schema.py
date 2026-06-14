"""Unit tests for schema introspection, diffing and body rendering."""

from __future__ import annotations

from typing import TypedDict

import pytest
from pydantic import BaseModel

from langmigrate.core.schema import diff_schema, introspect, load_schema, render_bodies


class TDState(TypedDict):
    messages: list
    count: int


class PydState(BaseModel):
    messages: list[str]
    count: int
    context: dict


def test_introspect_typeddict():
    assert introspect(TDState) == {"messages": "list", "count": "int"}


def test_introspect_pydantic():
    out = introspect(PydState)
    assert out["count"] == "int"
    assert out["messages"] == "list[str]"
    assert "context" in out


def test_introspect_plain_dict():
    assert introspect({"a": "int", "b": str}) == {"a": "int", "b": "str"}


def test_diff_added_removed_changed():
    old = {"a": "int", "b": "str", "c": "int"}
    new = {"a": "int", "b": "int", "d": "list"}
    diff = diff_schema(old, new)
    assert diff.added == {"d": "list"}
    assert diff.removed == {"c": "int"}
    assert diff.changed == {"b": ("str", "int")}
    assert not diff.is_empty


def test_diff_empty_when_identical():
    schema = {"a": "int"}
    assert diff_schema(schema, schema).is_empty


def test_render_bodies_added_field():
    up, down = render_bodies(diff_schema({}, {"context": "dict"}))
    assert any('add_field(state, "context"' in line for line in up)
    assert any('drop_field(state, "context")' in line for line in down)


def test_render_bodies_changed_type_builtin_coercion():
    up, down = render_bodies(diff_schema({"count": "str"}, {"count": "int"}))
    assert any('coerce_field(state, "count", int)' in line for line in up)
    assert any('coerce_field(state, "count", str)' in line for line in down)


def test_render_bodies_rename_hint_on_drop_and_add():
    up, _ = render_bodies(diff_schema({"old": "int"}, {"new": "int"}))
    assert any("rename" in line.lower() for line in up)


def test_render_bodies_empty_diff_emits_placeholder():
    # An empty diff still yields a syntactically valid (commented) body.
    up, down = render_bodies(diff_schema({"a": "int"}, {"a": "int"}))
    assert up == ["# No schema changes detected."]
    assert down == ["# No schema changes detected."]


def test_load_schema_rejects_ref_without_colon():
    with pytest.raises(ValueError, match="module.path:Attr"):
        load_schema("myapp.state")


def test_type_name_renders_unions_and_none():
    from langmigrate.core.schema import _type_name

    assert _type_name(None) == "None"
    assert _type_name(int | None) == "int | None"
    assert _type_name(int | str) == "int | str"


def test_introspect_rejects_non_schema_object():
    with pytest.raises(TypeError):
        introspect(42)


def test_type_name_annotated_with_type_metadata():
    from typing import Annotated

    from langmigrate.core.schema import _type_name

    # Metadata that is itself a type is rendered via its type name, not repr.
    assert _type_name(Annotated[int, str]) == "Annotated[int, str]"


def test_type_name_renders_bare_tuple_and_list():
    from langmigrate.core.schema import _type_name

    assert _type_name((int, str)) == "(int, str)"
    assert _type_name([int, str]) == "[int, str]"


def test_type_name_falls_back_to_str_for_unknown():
    from langmigrate.core.schema import _type_name

    # An annotation that is neither a typing construct, type, list/tuple nor
    # ForwardRef falls through to the str() rendering.
    assert _type_name(42) == "42"


def test_introspect_pydantic_falls_back_when_type_hints_fail(monkeypatch):
    # If get_type_hints raises (e.g. an unresolvable forward ref), introspection
    # must fall back to each field's raw annotation rather than crash.
    import langmigrate.core.schema as schema_mod

    def _boom(*args, **kwargs):
        raise NameError("unresolved forward ref")

    monkeypatch.setattr(schema_mod, "get_type_hints", _boom)
    # The fallback renders each field's raw annotation directly.
    assert introspect(PydState) == {"messages": "list[str]", "count": "int", "context": "dict"}


def test_render_bodies_emits_todo_for_non_builtin_downgrade():
    from langmigrate.core.schema import SchemaDiff

    # OLD type is non-builtin -> the downgrade coercion gets a TODO comment line.
    diff = SchemaDiff(changed={"tags": ("list[int]", "str")})
    up, down = render_bodies(diff)

    assert any("TODO" in line for line in down)
    assert not any("TODO" in line for line in up)  # new type 'str' is builtin
