"""Middleware path: migrate state inside a LangGraph node (managed platform).

This pattern applies when you **don't own the checkpointer** — e.g. LangGraph Server
/ Cloud, or any setup where you can't wrap the saver with ``MigrationInterceptor``.
Instead, a dedicated ``migrate`` node is inserted at the graph's entry point and calls
``migrate_state_update`` to apply any pending migrations.

The demo also shows how ``SchemaMigrationMiddleware`` automates this for agents that
declare middleware, and highlights the channel-removal limitation intrinsic to the
state-update approach.

Run:
    uv run python examples/middleware_agent/demo.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from langmigrate import (
    REVISION_METADATA_KEY,
    MigrationEngine,
    MigrationRegistry,
    migrate_state_update,
)

MIGRATIONS = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# State schema (head revision)
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    query: str
    response: str
    metadata: dict[str, Any]
    confidence_score: float
    model_id: str
    langmigrate_rev: str  # reserved channel for the state-level revision tag


# ---------------------------------------------------------------------------
# Demo helpers
# ---------------------------------------------------------------------------


def make_v0_state(query: str, response: str) -> dict[str, Any]:
    """Simulate a thread persisted before any schema revision."""
    return {"query": query, "response": response}


def make_v1_state(query: str, response: str) -> dict[str, Any]:
    """Simulate a thread persisted at revision a1c0 (has metadata, no confidence)."""
    return {
        "query": query,
        "response": response,
        "metadata": {},
        REVISION_METADATA_KEY: "a1c0",
    }


def show_state(label: str, state: dict[str, Any]) -> None:
    rev = state.get(REVISION_METADATA_KEY, "<untagged>")
    keys = [k for k in state if k != REVISION_METADATA_KEY]
    print(f"{label}")
    print(f"  revision : {rev!r}")
    print(f"  fields   : {keys}")
    print(f"  state    : { {k: state[k] for k in keys} }")
    print()


# ---------------------------------------------------------------------------
# Part 1 — migrate_state_update (the low-level primitive)
# ---------------------------------------------------------------------------


def demo_low_level() -> None:
    print("=" * 60)
    print("PART 1 — migrate_state_update (low-level primitive)")
    print("=" * 60, "\n")

    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    head = engine.head()
    print(f"Current head: {head!r}\n")

    # A v0 thread (no revision tag, no metadata, no confidence_score)
    stale_v0 = make_v0_state("What is the capital of France?", "Paris.")
    show_state("v0 thread (untagged, 2 fields):", stale_v0)

    update = migrate_state_update(engine, stale_v0)
    assert update is not None, "Expected an update — state was stale"
    print("Update returned by migrate_state_update:")
    for k, v in update.items():
        print(f"  {k!r}: {v!r}")
    print()

    # Apply the update (as LangGraph would via a node return)
    merged = {**stale_v0, **update}
    show_state("v0 thread after migration merged in:", merged)

    # A v1 thread (already at a1c0, only confidence_score missing)
    stale_v1 = make_v1_state("What is the speed of light?", "~3×10⁸ m/s.")
    show_state("v1 thread (revision=a1c0, missing confidence_score):", stale_v1)
    update2 = migrate_state_update(engine, stale_v1)
    assert update2 is not None
    merged2 = {**stale_v1, **update2}
    show_state("v1 thread after migration:", merged2)

    # Head-revision thread: migrate_state_update returns None (no-op)
    up_to_date = {**merged2}
    update3 = migrate_state_update(engine, up_to_date)
    print(f"Head-revision thread → migrate_state_update returns: {update3!r} (no-op)\n")


# ---------------------------------------------------------------------------
# Part 2 — using migrate_state_update inside a LangGraph node
# ---------------------------------------------------------------------------


def demo_node_pattern() -> None:
    print("=" * 60)
    print("PART 2 — migrate node pattern (LangGraph StateGraph)")
    print("=" * 60, "\n")

    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        print("  langgraph not installed — skipping LangGraph demo.\n")
        return

    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))

    def migrate_node(state: dict[str, Any]) -> dict[str, Any] | None:
        """Entry node: migrate state on every graph invocation (idempotent)."""
        return migrate_state_update(engine, state)

    def answer_node(state: dict[str, Any]) -> dict[str, Any]:
        """Simulate the agent producing an answer with a confidence score."""
        return {
            "response": f"Answer to: {state.get('query', '?')}",
            "confidence_score": 0.95,
            "model_id": "gpt-4o",
        }

    graph = StateGraph(AgentState)
    graph.add_node("migrate", migrate_node)
    graph.add_node("answer", answer_node)
    graph.set_entry_point("migrate")
    graph.add_edge("migrate", "answer")
    graph.add_edge("answer", END)
    app = graph.compile()

    # Feed a v0 thread — the migrate node upgrades it transparently
    v0_input = make_v0_state("Who wrote Hamlet?", "")
    print("Invoking graph with v0 state (no migration tag)...")
    result = app.invoke(v0_input)
    print(f"  final state revision : {result.get(REVISION_METADATA_KEY)!r}")
    print(f"  confidence_score     : {result.get('confidence_score')!r}")
    print(f"  response             : {result.get('response')!r}\n")

    print("Invoking again (head revision) — migrate node is a no-op:")
    result2 = app.invoke(result)
    print(f"  revision             : {result2.get(REVISION_METADATA_KEY)!r}")
    same_revision = result.get(REVISION_METADATA_KEY) == result2.get(REVISION_METADATA_KEY)
    print(f"  (same revision as before: {same_revision})\n")


# ---------------------------------------------------------------------------
# Part 3 — SchemaMigrationMiddleware (show the constructor; runtime optional)
# ---------------------------------------------------------------------------


def demo_middleware() -> None:
    print("=" * 60)
    print("PART 3 — SchemaMigrationMiddleware (managed-platform shortcut)")
    print("=" * 60, "\n")

    print("SchemaMigrationMiddleware wraps the migrate_node pattern automatically.")
    print("It hooks both before_agent (fresh start) and before_model (mid-loop resume).")
    print("Construction:")
    print("  from langmigrate.integrations.langchain import SchemaMigrationMiddleware")
    print('  middleware = SchemaMigrationMiddleware("path/to/migrations")')
    print("  agent = create_agent(model, tools=[...], middleware=[middleware])")
    print()
    print("The middleware.state_schema declares `langmigrate_rev` as a graph channel")
    print("so LangGraph can accept the tag update without a schema error.\n")

    print("LIMITATION: channel removal (rename/drop) cannot be applied via state")
    print("updates because LangGraph *merges* them — the old key lingers.")
    print("For hard channel removal → use MigrationInterceptor (see evolving_agent).\n")

    try:
        from langmigrate.integrations.langchain import SchemaMigrationMiddleware

        engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
        mw = SchemaMigrationMiddleware(engine)
        v0 = make_v0_state("Test query", "Test response")
        update = mw.before_agent(v0)
        print("SchemaMigrationMiddleware.before_agent on v0 state returned update:")
        for k, v in (update or {}).items():
            print(f"  {k!r}: {v!r}")
        print()
    except ImportError:
        print("(langchain not installed — SchemaMigrationMiddleware import skipped)\n")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    demo_low_level()
    demo_node_pattern()
    demo_middleware()


if __name__ == "__main__":
    main()
