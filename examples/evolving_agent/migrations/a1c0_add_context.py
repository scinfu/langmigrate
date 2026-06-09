"""v1: add a `context` dict with a safe default.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddContext(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_context"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Safe: lazily inject a default for a newly added field.
        return self.add_field(state, "context", factory=dict)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.drop_field(state, "context")
