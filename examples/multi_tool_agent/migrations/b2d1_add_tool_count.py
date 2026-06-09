"""v2: rename `user_input` → `query`, add `tool_calls_count` int.

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddToolCount(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "add_tool_count"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Rename the input channel (unsafe: data moves from one key to another).
        if "user_input" in state.values:
            state = self.rename_field(state, "user_input", "query")
        return self.add_field(state, "tool_calls_count", 0)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        if "query" in state.values:
            state = self.rename_field(state, "query", "user_input")
        return self.drop_field(state, "tool_calls_count")
