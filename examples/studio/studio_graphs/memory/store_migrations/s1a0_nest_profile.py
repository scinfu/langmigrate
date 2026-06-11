"""Store item v2: nest ``name`` under ``profile`` and add ``language``.

Revision ID: s1a0
Down revision: (base)

Store migrations live in their own directory because item shapes evolve
independently of checkpoint channel shapes. The transform is idempotent: a
value that already has ``profile`` passes through with only ``language``
defaulted.
"""

from __future__ import annotations

from langmigrate import StateEnvelope, migration


@migration("s1a0", down_revision=None, slug="nest_profile")
def nest_profile(state: StateEnvelope) -> StateEnvelope:
    values = dict(state.values)
    if "profile" not in values:
        values["profile"] = {"name": values.pop("name", "unknown")}
    else:
        values.pop("name", None)
    values.setdefault("language", "english")
    return state.with_values(values)


@nest_profile.reverse
def nest_profile_down(state: StateEnvelope) -> StateEnvelope:
    values = dict(state.values)
    profile = values.pop("profile", None)
    if profile is not None:
        values["name"] = profile.get("name", "unknown")
    values.pop("language", None)
    return state.with_values(values)
