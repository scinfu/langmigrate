"""v1: add `session_id` (UUID) and `iteration` counter.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

import uuid

from langmigrate import BaseMigration, StateEnvelope


class AddSession(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_session"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.add_field(state, "session_id", factory=lambda: str(uuid.uuid4()))
        return self.add_field(state, "iteration", 0)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "session_id")
        return self.drop_field(state, "iteration")
