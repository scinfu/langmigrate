"""Declarative field-level migration primitives.

Every function here is **pure** and **idempotent at the key level**: it takes a
:class:`StateEnvelope` and returns a new one, never mutating the input. Re-running
an *add*/*drop*/*rename* against already-migrated state is a no-op.

Safety classes (see the compatibility matrix in the README):

- ``add_field`` / ``drop_field`` — **Safe**.
- ``rename_field`` / ``coerce_field`` / ``require_field`` — **Unsafe**, handled here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .exceptions import MissingRequiredFieldError, UnsafeMigrationError
from .types import _OPS_UNSET, StateEnvelope

# Sentinel distinguishing "no literal default given" from ``default=None``. Shared
# with :class:`StateEnvelope`'s fluent helpers (see ``core.types``).
_UNSET = _OPS_UNSET


def strict_equal(a: Any, b: Any) -> bool:
    """Equality that treats a type change at ANY depth as a difference.

    ``1 == 1.0`` and ``0 == False`` are real changes for persistence (the stored
    blob differs), so plain ``==`` is not enough. Containers (dict/list/tuple) are
    compared recursively. Remaining limitation: a type change buried inside an
    *opaque* object (custom classes, sets — ``{1} == {1.0}``) is undetectable
    without serializer coupling; a migration needing that should return a
    structurally new value.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_equal(v, b[k]) for k, v in a.items())
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(map(strict_equal, a, b))
    return a == b


def add_field(
    state: StateEnvelope,
    name: str,
    default: Any = _UNSET,
    *,
    factory: Callable[[], Any] | None = None,
) -> StateEnvelope:
    """Safe: inject ``name`` with a default if it is absent (lazy default).

    Idempotent — if the field already exists its value is preserved. Provide either
    a literal ``default`` or a ``factory`` callable (mirrors dataclass semantics).
    """
    if (default is _UNSET) == (factory is None):
        raise ValueError("add_field requires exactly one of `default` or `factory`")
    if name in state.values:
        return state
    value = factory() if factory is not None else default
    return state.with_values({**state.values, name: value})


def drop_field(state: StateEnvelope, name: str) -> StateEnvelope:
    """Safe: remove ``name`` to clean up the payload. No-op if already absent."""
    if name not in state.values:
        return state
    new_values = dict(state.values)
    del new_values[name]
    return state.with_values(new_values)


def rename_field(state: StateEnvelope, old: str, new: str) -> StateEnvelope:
    """Unsafe: remap key ``old`` -> ``new`` without losing the value.

    Idempotent — if ``old`` is already gone the state is returned unchanged. Raises
    :class:`UnsafeMigrationError` if both keys are present with differing values, as
    that would silently drop data.
    """
    if old == new:
        return state
    if old not in state.values:
        return state
    if new in state.values and state.values[new] != state.values[old]:
        raise UnsafeMigrationError(
            f"Cannot rename {old!r} -> {new!r}: target already exists with a different value",
            field=new,
        )
    new_values = dict(state.values)
    new_values[new] = new_values.pop(old)
    return state.with_values(new_values)


def coerce_field(
    state: StateEnvelope,
    name: str,
    fn: Callable[[Any], Any],
    *,
    skip_if: Callable[[Any], bool] | None = None,
) -> StateEnvelope:
    """Unsafe: convert the type/shape of ``name`` via ``fn``. No-op if absent.

    ``skip_if`` lets a migration guard against re-coercion (e.g. ``skip_if=lambda v:
    isinstance(v, int)``), keeping repeated runs safe.
    """
    if name not in state.values:
        return state
    value = state.values[name]
    if skip_if is not None and skip_if(value):
        return state
    new_value = fn(value)
    # Strict (deep, type-aware) comparison: a coercion like 1 -> 1.0 nested inside
    # a container is a real change even though the values compare ``==``.
    if new_value is value or strict_equal(new_value, value):
        return state
    return state.with_values({**state.values, name: new_value})


def require_field(
    state: StateEnvelope,
    name: str,
    *,
    fallback: Any = _UNSET,
    factory: Callable[[], Any] | None = None,
    revision: str | None = None,
) -> StateEnvelope:
    """Unsafe: assert ``name`` exists; otherwise inject a fallback or block.

    If the field is present, the state is returned unchanged. If absent and a
    ``fallback`` value or ``factory`` is supplied, it is injected. If absent with no
    fallback, raises :class:`MissingRequiredFieldError` (a structured block).

    ``fallback`` and ``factory`` are mutually exclusive (as in :func:`add_field`).
    """
    if fallback is not _UNSET and factory is not None:
        raise ValueError("require_field accepts at most one of `fallback` or `factory`")
    if name in state.values:
        return state
    if factory is not None:
        return state.with_values({**state.values, name: factory()})
    if fallback is not _UNSET:
        return state.with_values({**state.values, name: fallback})
    raise MissingRequiredFieldError(name, revision=revision)
