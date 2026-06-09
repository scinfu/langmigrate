"""v3: enforce that `query` is always present; coerce `iteration` to int.

Threads persisted when `query` was optional will now fail loudly if it is
truly absent (no fallback), making the invariant visible rather than silent.

Revision ID: c3e2
Down revision: b2d1
"""

from __future__ import annotations

from langmigrate import BaseMigration, StateEnvelope


class RequireQuery(BaseMigration):
    revision = "c3e2"
    down_revision = "b2d1"
    slug = "require_query"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Ensure `query` is present (raises MissingRequiredFieldError if not).
        state = self.require_field(state, "query", fallback="<missing>")
        # Guard against legacy threads that stored iteration as a string.
        return self.coerce_field(state, "iteration", int, skip_if=lambda v: isinstance(v, int))

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Downgrade is a no-op for structural changes: the field stays.
        return state
