"""v2: normalise `status` to lowercase and add `attempt` counter.

Old threads have status written in mixed case (e.g. "Pending", "IN_PROGRESS").
This migration coerces them to lowercase for consistency.

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class NormaliseStatus(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "normalise_status"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.coerce_field(
            state,
            "status",
            lambda v: v.lower() if isinstance(v, str) else v,
            skip_if=lambda v: isinstance(v, str) and v == v.lower(),
        )
        return self.add_field(state, "attempt", 1)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Status normalisation cannot be truly reversed (original casing is lost).
        # We drop `attempt` and leave `status` as-is (lowercase).
        return self.drop_field(state, "attempt")
