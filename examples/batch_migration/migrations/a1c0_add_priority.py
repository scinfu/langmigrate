"""v1: add `priority` string field and `tags` list.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddPriorityAndTags(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_priority_and_tags"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.add_field(state, "priority", "normal")
        return self.add_field(state, "tags", factory=list)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "priority")
        return self.drop_field(state, "tags")
