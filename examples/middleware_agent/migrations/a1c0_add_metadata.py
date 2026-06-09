"""v1: add a `metadata` dict with a safe default.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddMetadata(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_metadata"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.add_field(state, "metadata", factory=dict)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.drop_field(state, "metadata")
