"""v3: drop `debug_info` field (irreversible — data discarded).

`debug_info` was a legacy field written during development. It is now removed
permanently. The downgrade is intentionally irreversible: once this migration
runs, the debug data is gone.

Revision ID: c3e2
Down revision: b2d1
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class DropDebugInfo(BaseMigration):
    revision = "c3e2"
    down_revision = "b2d1"
    slug = "drop_debug_info"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # drop_field is a no-op if the field is already absent.
        return self.drop_field(state, "debug_info")

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        # The data is gone — there is nothing to restore.
        self.raise_irreversible()
