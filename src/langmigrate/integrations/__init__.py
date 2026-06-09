"""Framework integrations for applying migrations at the *state* level.

Use these when you do **not** own the checkpointer instance (e.g. on LangGraph
Server / managed platforms): instead of wrapping the saver, the migration runs on
the deserialized state at the start of each step.

The pure helper :func:`~langmigrate.integrations.state.migrate_state_update` has no
third-party dependencies; ``langchain`` is only imported by the middleware shim.
"""

from .state import DEFAULT_STATE_REV_KEY, migrate_state_update

__all__ = ["DEFAULT_STATE_REV_KEY", "migrate_state_update"]
