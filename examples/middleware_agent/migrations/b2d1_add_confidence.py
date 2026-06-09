"""v2: add `confidence_score` float (default 0.0) and `model_id` string.

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddConfidenceAndModel(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "add_confidence_and_model"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.add_field(state, "confidence_score", 0.0)
        return self.add_field(state, "model_id", "gpt-4")

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "confidence_score")
        return self.drop_field(state, "model_id")
