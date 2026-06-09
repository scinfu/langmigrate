"""v2: add `findings` list and rename `research_step` → `web_researcher` node.

The graph was refactored in v2: the node formerly called ``research_step`` is now
``web_researcher``. Any thread interrupted on the old node is repaired here via
``remap_node`` so the next resume lands on the correct node.

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class AddFindingsAndRemapNode(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "add_findings_and_remap_node"

    # Current graph topology (after the v2 refactor).
    _KNOWN_NODES = ["planner", "web_researcher", "synthesizer", "reviewer", "__end__"]

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Add the new findings accumulator.
        state = self.add_field(state, "findings", factory=list)
        # Repair threads stuck on the old node name.
        state = self.remap_node(
            state,
            renames={"research_step": "web_researcher"},
            known_nodes=self._KNOWN_NODES,
            fallback="planner",  # unknown paused nodes fall back to planner
        )
        return state

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "findings")
        # Reverse the node remap (web_researcher → research_step).
        state = self.remap_node(state, renames={"web_researcher": "research_step"})
        return state
