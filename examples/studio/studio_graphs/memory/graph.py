"""Memory graph: store-item migration with ``MigrationStore`` in Studio.

Pattern: the LangGraph API server injects a managed ``BaseStore`` (cross-thread
memory). You can't replace it, but you *can* wrap it per call with
:func:`langmigrate.setup_langmigrate_store`: ``get``/``aget`` heal stale items
lazily (with write-back); the revision tag lives inside ``Item.value`` and is
stripped from everything the wrapper returns.

Chat commands (any thread — the store is shared across threads):

- ``save <name>``   — persist a profile item for the demo user
- anything else     — read the profile back

v1 items look like ``{"name": ...}``; v2 code expects
``{"profile": {"name": ...}, "language": ...}``.

Demo toggles — edit, save, and ``langgraph dev`` hot-reloads (the store survives):

1. ``SCHEMA_VERSION = 1`` / ``LANGMIGRATE_ENABLED = False`` — ``save Mario``, read it.
2. ``SCHEMA_VERSION = 2`` — reading raises ``SchemaOutOfDateError`` (v1 item shape).
3. ``LANGMIGRATE_ENABLED = True`` — read again: the item is migrated in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.config import get_store
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from typing_extensions import TypedDict

from langmigrate import setup_langmigrate_store

from ..common import SchemaOutOfDateError, last_human_text

# ---------------------------------------------------------------------------
# DEMO TOGGLES — edit these two lines during the walkthrough.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1  # 1 = flat {"name"} items, 2 = nested {"profile", "language"}
LANGMIGRATE_ENABLED = False  # True = MigrationStore heals stale items on get()

STORE_MIGRATIONS = Path(__file__).parent / "store_migrations"

NAMESPACE = ("studio", "profiles")
USER_KEY = "demo-user"


class MemoryState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _store() -> BaseStore:
    store = get_store()  # the platform-managed store injected by langgraph dev
    if LANGMIGRATE_ENABLED:
        store = setup_langmigrate_store(store, STORE_MIGRATIONS)
    return store


def respond(state: MemoryState) -> dict[str, Any]:
    text = last_human_text(state["messages"]).strip()
    store = _store()

    if text.lower().startswith(("save ", "ricorda ")):
        name = text.split(" ", 1)[1].strip() or "anonymous"
        if SCHEMA_VERSION == 1:
            value: dict[str, Any] = {"name": name}
        else:
            value = {"profile": {"name": name}, "language": "english"}
        store.put(NAMESPACE, USER_KEY, value)
        return {"messages": [AIMessage(f"[v{SCHEMA_VERSION}] Saved profile: {value!r}")]}

    item = store.get(NAMESPACE, USER_KEY)
    if item is None:
        return {
            "messages": [AIMessage("No profile yet. Say `save <name>` to store one (any thread).")]
        }

    if SCHEMA_VERSION == 1:
        name = item.value.get("name", "?")
        return {"messages": [AIMessage(f"[v1] Stored name: {name!r}")]}

    # v2 reads the nested shape strictly — old flat items fail here.
    if "profile" not in item.value:
        raise SchemaOutOfDateError(
            "The stored item still has the v1 shape {'name': ...} but the v2 code "
            "expects {'profile': {...}, 'language': ...}. "
            "Fix: set LANGMIGRATE_ENABLED = True in studio_graphs/memory/graph.py "
            "so MigrationStore migrates the item on get() (revision s1a0), then ask again."
        )
    name = item.value["profile"].get("name", "?")
    language = item.value.get("language", "?")
    return {"messages": [AIMessage(f"[v2] Stored name: {name!r} (language: {language})")]}


builder = StateGraph(MemoryState)
builder.add_node("respond", respond)
builder.add_edge(START, "respond")
builder.add_edge("respond", END)

graph = builder.compile()
