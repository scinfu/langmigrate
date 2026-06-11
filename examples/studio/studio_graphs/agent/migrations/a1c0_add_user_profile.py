"""v2: inject a default ``user_profile`` for threads created before v2.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import StateEnvelope, migration


def _default_profile() -> dict[str, str]:
    return {"name": "guest", "tone": "friendly"}


@migration("a1c0", down_revision=None, slug="add_user_profile")
def add_user_profile(state: StateEnvelope) -> StateEnvelope:
    return state.add_field("user_profile", factory=_default_profile)


@add_user_profile.reverse
def add_user_profile_down(state: StateEnvelope) -> StateEnvelope:
    return state.drop_field("user_profile")
