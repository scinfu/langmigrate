"""Multi-tool ReAct agent with LangGraph + MigrationInterceptor.

Demonstrates the **saver-wrapping** path for a realistic agent that uses tool
nodes. Three legacy threads (v0, v1, v2 schema) are seeded, then the same graph
resumes each through a ``MigrationInterceptor`` — no code changes to the agent.

Schema evolution:
    v0  : {user_input, plan, output}
    a1c0: + session_id (UUID), + iteration (int)
    b2d1: user_input → query, + tool_calls_count
    c3e2: require query, coerce iteration to int

Run:
    uv run python examples/multi_tool_agent/demo.py
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import (
    REVISION_METADATA_KEY,
    MigrationEngine,
    MigrationInterceptor,
    MigrationRegistry,
)

MIGRATIONS = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# Minimal agent graph (no LLM needed — simulates tool calling)
# ---------------------------------------------------------------------------


def _build_graph(interceptor: MigrationInterceptor) -> Any:
    """Build a tiny StateGraph that simulates a tool-calling loop."""
    try:
        from typing import TypedDict

        from langgraph.graph import END, StateGraph
    except ImportError:
        return None

    class AgentState(TypedDict, total=False):
        query: str
        plan: list[str]
        output: str
        session_id: str
        iteration: int
        tool_calls_count: int

    def planner(state: AgentState) -> dict[str, Any]:
        q = state.get("query", "")
        return {
            "plan": [f"step-1: search for '{q}'", "step-2: summarise"],
            "iteration": state.get("iteration", 0) + 1,
        }

    def tool_executor(state: AgentState) -> dict[str, Any]:
        count = state.get("tool_calls_count", 0)
        return {
            "tool_calls_count": count + len(state.get("plan", [])),
            "output": f"Tool result for: {state.get('query', '?')}",
        }

    def should_continue(state: AgentState) -> str:
        return "respond" if state.get("output") else "tool"

    def responder(state: AgentState) -> dict[str, Any]:
        return {"output": f"[FINAL] {state.get('output', '')}"}

    g = StateGraph(AgentState)
    g.add_node("planner", planner)
    g.add_node("tool", tool_executor)
    g.add_node("respond", responder)
    g.set_entry_point("planner")
    g.add_edge("planner", "tool")
    g.add_edge("tool", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=interceptor)


# ---------------------------------------------------------------------------
# Seed legacy threads at different schema versions
# ---------------------------------------------------------------------------


LEGACY_THREADS = [
    {
        "thread_id": "thread-v0",
        "values": {"user_input": "What causes thunder?", "plan": [], "output": ""},
        "meta": {},
        "label": "v0 thread (user_input, no session_id)",
    },
    {
        "thread_id": "thread-v1",
        "values": {
            "user_input": "How do vaccines work?",
            "plan": [],
            "output": "",
            "session_id": "aaaa-bbbb",
            "iteration": "2",  # stored as string in the wild
        },
        "meta": {REVISION_METADATA_KEY: "a1c0"},
        "label": "v1 thread (a1c0, iteration as string)",
    },
    {
        "thread_id": "thread-v2",
        "values": {
            "query": "Is Pluto a planet?",
            "plan": [],
            "output": "",
            "session_id": "cccc-dddd",
            "iteration": 1,
            "tool_calls_count": 0,
        },
        "meta": {REVISION_METADATA_KEY: "b2d1"},
        "label": "v2 thread (b2d1, missing require_query revision)",
    },
]


def seed_threads(saver: InMemorySaver) -> None:
    for t in LEGACY_THREADS:
        config = {"configurable": {"thread_id": t["thread_id"], "checkpoint_ns": ""}}
        chk = empty_checkpoint()
        chk["channel_values"] = t["values"]
        chk["channel_versions"] = dict.fromkeys(t["values"], 1)
        saver.put(config, chk, {"source": "loop", **t["meta"]}, dict.fromkeys(t["values"], 1))


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def main() -> None:
    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()
    seed_threads(saver)

    interceptor = MigrationInterceptor(saver, engine, write_back=True)
    app = _build_graph(interceptor)

    head = engine.head()
    print(f"Head revision: {head!r}")
    chain = engine.registry.lineage(head)
    print(f"Migration chain: {' → '.join(chain)}\n")

    for t in LEGACY_THREADS:
        config = {"configurable": {"thread_id": t["thread_id"], "checkpoint_ns": ""}}

        # Show the raw (stale) state as it sits in the DB
        raw = saver.get_tuple(config)
        raw_rev = (raw.metadata or {}).get(REVISION_METADATA_KEY, "<untagged>")
        print(f"{'─' * 60}")
        print(f"Thread: {t['label']}")
        print(f"  Raw revision : {raw_rev!r}")
        print(f"  Raw fields   : {list(raw.checkpoint['channel_values'].keys())}")

        # Load through the interceptor (lazy upgrade + write-back)
        migrated = interceptor.get_tuple(config)
        mig_rev = (migrated.metadata or {}).get(REVISION_METADATA_KEY)
        mig_vals = migrated.checkpoint["channel_values"]
        print("  After migration:")
        print(f"    revision       : {mig_rev!r}  (== head: {mig_rev == head})")
        print(f"    fields         : {list(mig_vals.keys())}")
        print(f"    session_id     : {mig_vals.get('session_id', '<none>')[:8]}...")
        iteration = mig_vals.get("iteration")
        print(f"    iteration      : {iteration!r} (type: {type(iteration).__name__})")
        print(f"    tool_calls_count: {mig_vals.get('tool_calls_count')!r}")
        print(f"    query          : {mig_vals.get('query')!r}")

        print()

    print("All threads self-healed via write-back — DB is now at head revision.")

    if app is not None:
        # Run one fresh thread (new ID) to confirm the schema is fully compatible.
        fresh_config = {"configurable": {"thread_id": "fresh-thread", "checkpoint_ns": ""}}
        out = app.invoke({"query": "What is LangMigrate?"}, fresh_config)
        print("\nFresh graph run (new thread, head schema):")
        print(f"  output         : {textwrap.shorten(out.get('output', ''), 60)!r}")
        print(f"  tool_calls_cnt : {out.get('tool_calls_count')!r}")
        print(f"  iteration      : {out.get('iteration')!r}")


if __name__ == "__main__":
    main()
