"""v1: add `depth` (research recursion level) and `sub_topics` list.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddDepthAndSubTopics(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_depth_and_subtopics"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.add_field(state, "depth", 1)
        return self.add_field(state, "sub_topics", factory=list)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "depth")
        return self.drop_field(state, "sub_topics")
