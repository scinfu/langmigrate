"""Schema introspection and diffing for ``revision --autogenerate``.

A *schema* here is a flat mapping ``{field_name: type_repr}`` describing a
LangGraph state. It can be introspected from a ``TypedDict``, a Pydantic v2
model, or a plain ``dict``. Comparing the previous revision's snapshot against the
current code's schema yields the operations a new migration should perform.

Autogenerate is a *starting point*: renames cannot be detected reliably (they look
like a drop + an add) and defaults/coercions need human review. The generated body
is explicit and editable.
"""

from __future__ import annotations

import importlib
import types
import typing
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin, get_type_hints

# Builtin types we can emit a direct coercion callable for.
_BUILTIN_COERCIONS = {"int", "str", "float", "bool"}


def _metadata_repr(meta: Any) -> str:
    """Stable string for ``Annotated`` metadata.

    Callables (e.g. LangGraph reducers like ``add_messages``) are rendered by
    qualified name rather than ``repr`` — ``repr`` of a function embeds its memory
    address, which changes every process and would make persisted schema snapshots
    diff as "changed" on each autogenerate run.
    """
    if isinstance(meta, type):
        return _type_name(meta)
    if callable(meta):
        return getattr(meta, "__qualname__", None) or getattr(meta, "__name__", None) or repr(meta)
    return repr(meta)


def _type_name(annotation: Any) -> str:
    """Best-effort readable string for a type annotation (e.g. ``list[str]``)."""
    if annotation is None or annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    # Render unions (PEP 604 ``a | b`` and ``typing.Union``) as ``a | b``.
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(_type_name(a) for a in get_args(annotation))
    if origin is typing.Annotated:
        args = get_args(annotation)
        base = _type_name(args[0])
        metadata = ", ".join(_metadata_repr(m) for m in args[1:])
        return f"Annotated[{base}, {metadata}]"
    if origin is typing.Literal:
        joined_args = ", ".join(repr(a) for a in get_args(annotation))
        return f"Literal[{joined_args}]"
    if origin is not None:
        joined_args = ", ".join(_type_name(a) for a in get_args(annotation))
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{joined_args}]" if joined_args else name
    if isinstance(annotation, list):
        return "[" + ", ".join(_type_name(a) for a in annotation) + "]"
    if isinstance(annotation, tuple):
        return "(" + ", ".join(_type_name(a) for a in annotation) + ")"
    if isinstance(annotation, type):
        return annotation.__name__
    if isinstance(annotation, typing.ForwardRef):
        return annotation.__forward_arg__
    return str(annotation).replace("typing.", "")


def introspect(target: Any) -> dict[str, str]:
    """Return ``{field: type_repr}`` for a TypedDict, Pydantic model, or dict."""
    # Pydantic v2 model.
    model_fields = getattr(target, "model_fields", None)
    if isinstance(model_fields, dict) and model_fields:
        try:
            resolved = get_type_hints(target, include_extras=True)
            return {
                name: _type_name(resolved.get(name, f.annotation))
                for name, f in model_fields.items()
            }
        except Exception:
            return {name: _type_name(f.annotation) for name, f in model_fields.items()}
    # TypedDict / annotated class. Use get_type_hints to resolve string/forward-ref
    # annotations (e.g. under `from __future__ import annotations`).
    annotations = getattr(target, "__annotations__", None)
    if isinstance(annotations, dict) and annotations:
        try:
            resolved = get_type_hints(target, include_extras=True)
        except Exception:
            resolved = annotations
        return {name: _type_name(resolved.get(name, ann)) for name, ann in annotations.items()}
    # Plain mapping of name -> type (or type string).
    if isinstance(target, dict):
        return {
            name: (value if isinstance(value, str) else _type_name(value))
            for name, value in target.items()
        }
    raise TypeError(f"Cannot introspect schema from {target!r}")


def load_schema(ref: str) -> dict[str, str]:
    """Import ``"module.path:Attr"`` and introspect the referenced schema object."""
    if ":" not in ref:
        raise ValueError(f"Schema ref must be 'module.path:Attr', got {ref!r}")
    module_path, attr_path = ref.split(":", 1)
    module = importlib.import_module(module_path)
    target = module
    for part in attr_path.split("."):
        target = getattr(target, part)
    return introspect(target)


@dataclass
class SchemaDiff:
    """Field-level differences between an old and a new schema."""

    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_schema(old: dict[str, str], new: dict[str, str]) -> SchemaDiff:
    """Compute added / removed / type-changed fields going from ``old`` to ``new``."""
    diff = SchemaDiff()
    for name, type_repr in new.items():
        if name not in old:
            diff.added[name] = type_repr
        elif old[name] != type_repr:
            diff.changed[name] = (old[name], type_repr)
    for name, type_repr in old.items():
        if name not in new:
            diff.removed[name] = type_repr
    return diff


def _coercion_expr(type_repr: str) -> tuple[str, str | None]:
    """``(callable_expr, todo_comment)`` to coerce to ``type_repr``.

    For a builtin target the expression is the type itself (e.g. ``int``) and
    there is no TODO. For anything else the expression is a conservative
    identity ``lambda v: v`` and the TODO is returned **separately** so the
    caller can emit it on its own line: inlining ``# TODO ...`` after the lambda
    would comment out the rest of the generated statement — including the
    closing ``)`` of ``coerce_field(...)`` — producing an unparseable file.
    """
    if type_repr in _BUILTIN_COERCIONS:
        return type_repr, None
    return "lambda v: v", f"# TODO: implement manual coercion to {type_repr}"


def render_bodies(diff: SchemaDiff) -> tuple[list[str], list[str]]:
    """Return ``(upgrade_lines, downgrade_lines)`` implementing ``diff``.

    Each list contains statement lines (without indentation) that reassign
    ``state``. The caller indents and inserts them into the migration template.
    """
    up: list[str] = []
    down: list[str] = []

    for name, type_repr in diff.added.items():
        up.append(f'state = self.add_field(state, "{name}", default=None)  # {type_repr}')
        down.append(f'state = self.drop_field(state, "{name}")')
    for name, type_repr in diff.removed.items():
        up.append(f'state = self.drop_field(state, "{name}")')
        down.append(f'state = self.add_field(state, "{name}", default=None)  # {type_repr}')
    for name, (old_type, new_type) in diff.changed.items():
        up_expr, up_todo = _coercion_expr(new_type)
        if up_todo:
            up.append(up_todo)
        up.append(f'state = self.coerce_field(state, "{name}", {up_expr})')
        down_expr, down_todo = _coercion_expr(old_type)
        if down_todo:
            down.append(down_todo)
        down.append(f'state = self.coerce_field(state, "{name}", {down_expr})')

    if diff.removed and diff.added:
        note = (
            "# NOTE: a removed+added pair may actually be a rename; consider self.rename_field()."
        )
        up.insert(0, note)
        down.insert(0, note)

    if not up:
        up.append("# No schema changes detected.")
    if not down:
        down.append("# No schema changes detected.")
    return up, down
