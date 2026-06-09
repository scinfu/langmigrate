"""v2: rename `msgs` -> `messages` and coerce `count` to int.

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class RenameMsgs(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "rename_msgs"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Unsafe operations, handled explicitly: a key remap and a type coercion.
        state = self.rename_field(state, "msgs", "messages")
        return self.coerce_field(state, "count", int, skip_if=lambda v: isinstance(v, int))

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.rename_field(state, "messages", "msgs")
        return self.coerce_field(state, "count", str)
