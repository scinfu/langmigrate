"""Unit tests for the declarative field operations (Safe vs Unsafe)."""

from __future__ import annotations

import pytest

from langmigrate.core import operations as ops
from langmigrate.core.exceptions import MissingRequiredFieldError, UnsafeMigrationError
from langmigrate.core.types import StateEnvelope


def env(**values) -> StateEnvelope:
    return StateEnvelope(values=dict(values))


# --- add_field (Safe) -----------------------------------------------------


def test_add_field_injects_default_when_absent():
    out = ops.add_field(env(a=1), "b", default=5)
    assert out.values == {"a": 1, "b": 5}


def test_add_field_preserves_existing_value_idempotent():
    out = ops.add_field(env(a=1, b=99), "b", default=5)
    assert out.values["b"] == 99


def test_add_field_factory():
    out = ops.add_field(env(), "items", factory=list)
    assert out.values == {"items": []}
    # distinct factory invocations must not share state
    other = ops.add_field(env(), "items", factory=list)
    out.values["items"].append(1)
    assert other.values["items"] == []


def test_add_field_requires_exactly_one_of_default_or_factory():
    with pytest.raises(ValueError):
        ops.add_field(env(), "b")
    with pytest.raises(ValueError):
        ops.add_field(env(), "b", default=1, factory=list)


def test_add_field_does_not_mutate_input():
    src = env(a=1)
    ops.add_field(src, "b", default=2)
    assert src.values == {"a": 1}


# --- drop_field (Safe) ----------------------------------------------------


def test_drop_field_removes_key():
    out = ops.drop_field(env(a=1, b=2), "b")
    assert out.values == {"a": 1}


def test_drop_field_noop_when_absent():
    out = ops.drop_field(env(a=1), "b")
    assert out.values == {"a": 1}


# --- rename_field (Unsafe) ------------------------------------------------


def test_rename_field_moves_value():
    out = ops.rename_field(env(old=7), "old", "new")
    assert out.values == {"new": 7}


def test_rename_field_idempotent_when_old_absent():
    out = ops.rename_field(env(new=7), "old", "new")
    assert out.values == {"new": 7}


def test_rename_field_conflict_raises():
    with pytest.raises(UnsafeMigrationError):
        ops.rename_field(env(old=1, new=2), "old", "new")


def test_rename_field_same_value_collision_is_noop_safe():
    out = ops.rename_field(env(old=2, new=2), "old", "new")
    assert out.values == {"new": 2}


# --- coerce_field (Unsafe) ------------------------------------------------


def test_coerce_field_applies_fn():
    out = ops.coerce_field(env(n="42"), "n", int)
    assert out.values == {"n": 42}


def test_coerce_field_noop_when_absent():
    out = ops.coerce_field(env(), "n", int)
    assert out.values == {}


def test_coerce_field_skip_if_guards_reapplication():
    out = ops.coerce_field(env(n=42), "n", int, skip_if=lambda v: isinstance(v, int))
    assert out.values == {"n": 42}


# --- require_field (Unsafe) -----------------------------------------------


def test_require_field_passes_when_present():
    out = ops.require_field(env(x=1), "x")
    assert out.values == {"x": 1}


def test_require_field_injects_fallback():
    out = ops.require_field(env(), "x", fallback=0)
    assert out.values == {"x": 0}


def test_require_field_factory():
    out = ops.require_field(env(), "x", factory=dict)
    assert out.values == {"x": {}}


def test_require_field_rejects_both_fallback_and_factory():
    with pytest.raises(ValueError):
        ops.require_field(env(), "x", fallback=0, factory=dict)


def test_require_field_blocks_without_fallback():
    with pytest.raises(MissingRequiredFieldError) as ei:
        ops.require_field(env(), "x", revision="abc123")
    assert ei.value.field == "x"
    assert ei.value.revision == "abc123"
