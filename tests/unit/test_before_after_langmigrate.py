"""Before/after tests: reproduce the exact failures from the README on a *real*
LangGraph graph, then show the very same resume succeeds once LangMigrate wraps
the saver.

Each test has two phases:

1. **Before** — an interrupted thread persisted under the *old* schema is resumed
   on the *new* code. Without migration LangGraph rebuilds state from the stored
   channels and blows up exactly as documented in ``README.md``
   (``ValidationError`` / ``KeyError``).
2. **After** — the *same* base saver is wrapped in a
   :class:`~langmigrate.runtime.interceptor.MigrationInterceptor`; the lazy upgrade
   injects/renames the channel on load and the resume completes cleanly.

These exercise the public API (``migration`` + ``MigrationEngine`` +
``MigrationInterceptor``) end-to-end against ``langgraph``'s ``InMemorySaver``.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from langmigrate import (
    MigrationEngine,
    MigrationInterceptor,
    MigrationRegistry,
    NodeRemap,
    StateEnvelope,
    migration,
)
from langmigrate.core.exceptions import TopologyMismatchError

THREAD = {"configurable": {"thread_id": "legacy-thread"}}


# --------------------------------------------------------------------------- #
# Symptom 1: pydantic ValidationError — a required field added after the
# checkpoint was written is absent on resume (README "Field required [missing]").
# --------------------------------------------------------------------------- #


class AgentStateV1(BaseModel):
    """Original schema: only messages."""

    messages: list[str] = []


class AgentStateV2(BaseModel):
    """Evolved schema: ``user_id`` is now required (no default)."""

    messages: list[str] = []
    user_id: str


def _persist_interrupted_v1(saver: InMemorySaver) -> None:
    """Run a v1 graph that interrupts before its node, leaving a stale checkpoint."""

    def respond(state: AgentStateV1) -> dict:
        return {"messages": state.messages + ["ok"]}

    graph = StateGraph(AgentStateV1)
    graph.add_node("respond", respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    app = graph.compile(checkpointer=saver, interrupt_before=["respond"])
    app.invoke({"messages": ["resume me"]}, THREAD)


def _v2_app(saver):
    def respond(state: AgentStateV2) -> dict:
        return {"messages": state.messages + [f"hi {state.user_id}"]}

    graph = StateGraph(AgentStateV2)
    graph.add_node("respond", respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=saver)


def _add_user_id_engine() -> MigrationEngine:
    @migration("a1c0", down_revision=None, slug="add_user_id")
    def add_user_id(state):
        # Backfill the new required field on legacy state.
        return state.add_field("user_id", default="anonymous")

    @add_user_id.reverse
    def _(state):
        return state.drop_field("user_id")

    return MigrationEngine(MigrationRegistry.from_migrations([add_user_id]))


def test_missing_required_field_fails_then_works_with_langmigrate():
    # --- before: resume on the new schema without migration -> ValidationError ---
    base = InMemorySaver()
    _persist_interrupted_v1(base)

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="user_id"):
        _v2_app(base).invoke(None, THREAD)

    # --- after: same saver, now wrapped by LangMigrate -> resume succeeds ---
    saver = MigrationInterceptor(base, _add_user_id_engine())
    out = _v2_app(saver).invoke(None, THREAD)

    assert out["user_id"] == "anonymous"
    assert out["messages"] == ["resume me", "hi anonymous"]


# --------------------------------------------------------------------------- #
# Symptom 2: KeyError — a node reads a field that was *renamed* between deploys,
# on a thread persisted under the old name (README "KeyError: '<field>'").
# --------------------------------------------------------------------------- #


class StateV1(TypedDict):
    msgs: list[str]


class StateV2(TypedDict):
    messages: list[str]


def _persist_interrupted_renamed(saver: InMemorySaver) -> None:
    def step(state: StateV1) -> dict:
        return {"msgs": state["msgs"] + ["ok"]}

    graph = StateGraph(StateV1)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    app = graph.compile(checkpointer=saver, interrupt_before=["step"])
    app.invoke({"msgs": ["resume me"]}, THREAD)


def _v2_renamed_app(saver):
    def step(state: StateV2) -> dict:
        # Reads the *new* key; on a legacy thread that key does not exist.
        return {"messages": state["messages"] + ["ok2"]}

    graph = StateGraph(StateV2)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=saver)


def _rename_engine() -> MigrationEngine:
    @migration("b2d1", down_revision=None, slug="rename_msgs")
    def rename_msgs(state):
        return state.rename_field("msgs", "messages")

    @rename_msgs.reverse
    def _(state):
        return state.rename_field("messages", "msgs")

    return MigrationEngine(MigrationRegistry.from_migrations([rename_msgs]))


def test_renamed_field_keyerror_then_works_with_langmigrate():
    # --- before: node reads the renamed key on a legacy thread -> KeyError ---
    base = InMemorySaver()
    _persist_interrupted_renamed(base)

    with pytest.raises(KeyError, match="messages"):
        _v2_renamed_app(base).invoke(None, THREAD)

    # --- after: LangMigrate remaps msgs -> messages on load -> resume succeeds ---
    saver = MigrationInterceptor(base, _rename_engine())
    out = _v2_renamed_app(saver).invoke(None, THREAD)

    assert out["messages"] == ["resume me", "ok2"]
    assert "msgs" not in out  # the interceptor rebuilds channel_values, purging the old key


def test_write_back_heals_the_stored_checkpoint():
    """After one resume through the interceptor, the DB row is migrated in place
    (same checkpoint id), so a later raw read no longer carries the legacy key."""
    base = InMemorySaver()
    _persist_interrupted_renamed(base)
    legacy = base.get_tuple(THREAD)
    assert "msgs" in legacy.checkpoint["channel_values"]

    saver = MigrationInterceptor(base, _rename_engine(), write_back=True)
    saver.get_tuple(THREAD)  # lazy upgrade + write-back

    healed = base.get_tuple(THREAD)
    assert healed.checkpoint["id"] == legacy.checkpoint["id"]
    assert "messages" in healed.checkpoint["channel_values"]
    assert "msgs" not in healed.checkpoint["channel_values"]


# --------------------------------------------------------------------------- #
# Symptom 3: topology drift — a thread interrupted on a node that a later graph
# version *removed*. Unlike 1 & 2 this fails *silently*: LangGraph raises nothing,
# the pending ``interrupt()`` decision is dropped and stale state is returned
# (README documents this honestly as a silent failure). LangMigrate's NodeRemap
# turns that into either a correct redirect or a loud, structured error.
# --------------------------------------------------------------------------- #


class TopoState(TypedDict):
    value: int
    log: list[str]


def _persist_interrupted_on_old_node(saver: InMemorySaver):
    """v1 pipeline a -> old_node, paused (interrupted) right before ``old_node``."""

    def a(state: TopoState) -> dict:
        return {"log": state["log"] + ["a"]}

    def old_node(state: TopoState) -> dict:
        return {"value": state["value"] + 100, "log": state["log"] + ["old_node"]}

    graph = StateGraph(TopoState)
    graph.add_node("a", a)
    graph.add_node("old_node", old_node)
    graph.add_edge(START, "a")
    graph.add_edge("a", "old_node")
    graph.add_edge("old_node", END)
    app = graph.compile(checkpointer=saver, interrupt_before=["old_node"])
    app.invoke({"value": 1, "log": []}, THREAD)
    return app


def _v2_app_without_old_node(saver):
    """v2 graph where ``old_node`` is gone, replaced by ``new_node``."""

    def a(state: TopoState) -> dict:
        return {"log": state["log"] + ["a"]}

    def new_node(state: TopoState) -> dict:
        return {"value": state["value"] + 999, "log": state["log"] + ["new_node"]}

    graph = StateGraph(TopoState)
    graph.add_node("a", a)
    graph.add_node("new_node", new_node)
    graph.add_edge(START, "a")
    graph.add_edge("a", "new_node")
    graph.add_edge("new_node", END)
    return graph.compile(checkpointer=saver)


def test_removed_node_fails_silently_then_langmigrate_makes_it_resolvable():
    # --- before: resume on a graph missing the paused node -> SILENT failure ---
    base = InMemorySaver()
    v1 = _persist_interrupted_on_old_node(base)
    assert v1.get_state(THREAD).next == ("old_node",)  # paused on old_node

    v2 = _v2_app_without_old_node(base)
    out = v2.invoke(None, THREAD)  # no exception is raised...

    # ...but the interrupt decision was silently dropped: neither old_node (+100)
    # nor new_node (+999) ran, stale state is returned and `next` is now empty.
    assert out == {"value": 1, "log": ["a"]}
    assert v2.get_state(THREAD).next == ()

    # --- after: LangMigrate's NodeRemap makes the orphaned node explicit ---
    known_nodes = {"a", "new_node"}
    stuck = StateEnvelope(values={"value": 1, "log": ["a"]}, node="old_node")

    # A declared mapping redirects the stuck thread to its replacement node...
    remap = NodeRemap(renames={"old_node": "new_node"})
    assert remap.apply(stuck, known_nodes=known_nodes).node == "new_node"

    # ...and an *undeclared* orphan is blocked loudly instead of failing silently.
    with pytest.raises(TopologyMismatchError, match="old_node"):
        NodeRemap().apply(stuck, known_nodes=known_nodes)
