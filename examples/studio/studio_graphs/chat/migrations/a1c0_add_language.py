"""v2 step 1: add the ``language`` channel with a safe default.

Revision ID: a1c0
Down revision: (base)
"""

from __future__ import annotations

from langmigrate import StateEnvelope, migration


@migration("a1c0", down_revision=None, slug="add_language")
def add_language(state: StateEnvelope) -> StateEnvelope:
    return state.add_field("language", default="english")


@add_language.reverse
def add_language_down(state: StateEnvelope) -> StateEnvelope:
    return state.drop_field("language")
