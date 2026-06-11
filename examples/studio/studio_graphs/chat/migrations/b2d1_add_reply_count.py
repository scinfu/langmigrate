"""v2 step 2: add the ``reply_count`` counter (shows the cascade a1c0 -> b2d1).

Revision ID: b2d1
Down revision: a1c0
"""

from __future__ import annotations

from langmigrate import StateEnvelope, migration


@migration("b2d1", down_revision="a1c0", slug="add_reply_count")
def add_reply_count(state: StateEnvelope) -> StateEnvelope:
    return state.add_field("reply_count", default=0)


@add_reply_count.reverse
def add_reply_count_down(state: StateEnvelope) -> StateEnvelope:
    return state.drop_field("reply_count")
