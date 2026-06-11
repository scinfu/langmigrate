"""Chat graph: a dedicated ``migrate`` entry node heals stale threads lazily.

Pattern: you do NOT own the checkpointer in LangGraph Studio / Server, so the
``MigrationInterceptor`` (saver wrap) is not available. Instead the graph's entry
node calls :func:`langmigrate.migrate_state_update`, which applies the pending
revision cascade and returns only the state *update* to merge. The revision tag
lives in the reserved ``langmigrate_rev`` state channel.

Demo toggles — edit, save, and ``langgraph dev`` hot-reloads (threads survive):

1. ``SCHEMA_VERSION = 1`` / ``LANGMIGRATE_ENABLED = False`` — chat, create threads.
2. ``SCHEMA_VERSION = 2`` — resume an old thread: ``SchemaOutOfDateError``.
3. ``LANGMIGRATE_ENABLED = True`` — resume again: the thread is migrated and works.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import NotRequired, TypedDict

from langmigrate import MigrationEngine, MigrationRegistry, migrate_state_update

from ..common import SchemaOutOfDateError, last_human_text

# ---------------------------------------------------------------------------
# DEMO TOGGLES — edit these two lines during the walkthrough.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1  # 1 = original schema, 2 = adds `language` + `reply_count`
LANGMIGRATE_ENABLED = False  # True = the `migrate` node heals stale threads

MIGRATIONS = Path(__file__).parent / "migrations"
ENGINE = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))

GREETINGS = {
    "english": "Hello!",
    "italian": "Ciao!",
    "french": "Salut!",
    "spanish": "¡Hola!",
}


class ChatState(TypedDict):
    """Head-revision (v2) schema — a superset of v1, so old threads still load."""

    messages: Annotated[list[AnyMessage], add_messages]
    language: NotRequired[str]  # added by revision a1c0
    reply_count: NotRequired[int]  # added by revision b2d1
    langmigrate_rev: NotRequired[str]  # reserved channel for the revision tag


def migrate(state: ChatState) -> dict[str, Any] | None:
    """Entry node: bring the thread state up to the head revision (idempotent).

    Returns only added/changed channels — never ``messages``, so the
    ``add_messages`` reducer is not disturbed.
    """
    if not LANGMIGRATE_ENABLED:
        return None
    return migrate_state_update(ENGINE, state)


def respond(state: ChatState) -> dict[str, Any]:
    """Echo bot. The v2 code path *requires* the fields added by the migrations."""
    text = last_human_text(state["messages"])

    if SCHEMA_VERSION == 1:
        return {"messages": [AIMessage(f"[v1] You said: {text!r}")]}

    # v2 reads the new fields strictly — exactly what real evolved code does.
    if "language" not in state or "reply_count" not in state:
        raise SchemaOutOfDateError(
            "This thread was persisted with schema v1 and is missing the "
            "'language' / 'reply_count' channels required by v2. "
            "Fix: set LANGMIGRATE_ENABLED = True in studio_graphs/chat/graph.py "
            "so the `migrate` node upgrades the thread, then resume it."
        )

    language = state["language"]
    count = state["reply_count"] + 1
    greeting = GREETINGS.get(language, GREETINGS["english"])
    reply = f"[v2 · {language} · reply #{count}] {greeting} You said: {text!r}"
    return {"messages": [AIMessage(reply)], "reply_count": count}


builder = StateGraph(ChatState)
builder.add_node("migrate", migrate)
builder.add_node("respond", respond)
builder.add_edge(START, "migrate")
builder.add_edge("migrate", "respond")
builder.add_edge("respond", END)

# No checkpointer here: the LangGraph API server (Studio) provides its own.
graph = builder.compile()
