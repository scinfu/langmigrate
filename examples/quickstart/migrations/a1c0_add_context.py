"""v1: add a ``context`` dict — written with the ``@migration`` decorator.

Revision ID: a1c0
Down revision: (base)

This is the low-boilerplate, function-pair style: no ``BaseMigration`` subclass.
The upgrade is the decorated function; the downgrade is attached with ``.reverse``.
Mutations go through the fluent ``StateEnvelope`` helpers (``state.add_field(...)``).
"""

from __future__ import annotations

from langmigrate import StateEnvelope, migration


@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state: StateEnvelope) -> StateEnvelope:
    # Safe: lazily inject a default for a newly added field.
    return state.add_field("context", factory=dict)


@add_context.reverse
def add_context_down(state: StateEnvelope) -> StateEnvelope:
    return state.drop_field("context")
