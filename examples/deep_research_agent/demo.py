"""Deep research agent: supervisor + subgraph with topology migration.

Demonstrates three advanced LangMigrate features in a realistic multi-node
research agent scenario:

1. **NodeRemap** — a thread interrupted on the renamed ``research_step`` node
   is automatically redirected to its replacement ``web_researcher`` at migration
   time (no manual DB surgery needed).

2. **IrreversibleMigrationError** — the ``c3e2`` migration drops ``debug_info``
   permanently. Attempting a downgrade past it raises ``IrreversibleMigrationError``
   rather than silently losing data.

3. **Partial upgrade / downgrade targeting** — the engine can stop at any intermediate
   revision, useful for staged rollouts or A/B experiments.

Graph structure (v2):
    planner → web_researcher → synthesizer → reviewer → END
    (v1 called the second node ``research_step``)

Run:
    uv run python examples/deep_research_agent/demo.py
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from langmigrate import (
    REVISION_METADATA_KEY,
    IrreversibleMigrationError,
    MigrationEngine,
    MigrationInterceptor,
    MigrationRegistry,
    NodeRemap,
)

MIGRATIONS = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed(
    saver: InMemorySaver,
    thread_id: str,
    values: dict,
    meta: dict,
    paused_on: str | None = None,
) -> None:
    """Write a raw checkpoint as if it were persisted by an old version."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    chk = empty_checkpoint()
    chk["channel_values"] = values
    chk["channel_versions"] = dict.fromkeys(values, 1)
    if paused_on:
        # LangGraph stores the paused-on node in the checkpoint's `channel_values`
        # under the special `__next__` key in some versions, but here we set it
        # on the envelope directly via a custom metadata key for illustration.
        meta["__paused_node__"] = paused_on
    saver.put(config, chk, {"source": "loop", **meta}, dict.fromkeys(values, 1))


# ---------------------------------------------------------------------------
# Part 1 — NodeRemap: repair interrupted threads
# ---------------------------------------------------------------------------


def demo_node_remap() -> None:
    print("=" * 60)
    print("PART 1 — NodeRemap: thread paused on a renamed node")
    print("=" * 60, "\n")

    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()

    # Thread interrupted on the OLD node name "research_step" (v0/v1 schema)
    _seed(
        saver,
        "interrupted-old-node",
        {"topic": "Quantum entanglement", "sources": [], "summary": "", "debug_info": "v1-debug"},
        {},
        paused_on="research_step",
    )

    interceptor = MigrationInterceptor(saver, engine, write_back=True)
    old_node_cfg = {"configurable": {"thread_id": "interrupted-old-node", "checkpoint_ns": ""}}
    raw = saver.get_tuple(old_node_cfg)
    print("RAW checkpoint (v0, paused on 'research_step'):")
    print(f"  fields    : {list(raw.checkpoint['channel_values'].keys())}")
    print(f"  revision  : {(raw.metadata or {}).get(REVISION_METADATA_KEY, '<untagged>')!r}")
    print(f"  paused_on : {(raw.metadata or {}).get('__paused_node__')!r}\n")

    # Show NodeRemap directly (the migration calls it internally)
    remap = NodeRemap(renames={"research_step": "web_researcher"}, fallback="planner")
    from langmigrate import StateEnvelope

    env_paused = StateEnvelope(
        values=raw.checkpoint["channel_values"],
        node="research_step",
    )
    repaired = remap.apply(env_paused)
    print(f"NodeRemap.apply('research_step') → node is now: {repaired.node!r}\n")

    # Now through the interceptor (which triggers the b2d1 migration internally)
    migrated = interceptor.get_tuple(old_node_cfg)
    mig_rev = (migrated.metadata or {}).get(REVISION_METADATA_KEY)
    print("After MigrationInterceptor.get_tuple:")
    print(f"  revision : {mig_rev!r}  (== head: {mig_rev == engine.head()})")
    print(f"  fields   : {list(migrated.checkpoint['channel_values'].keys())}")
    print(f"  debug_info present: {'debug_info' in migrated.checkpoint['channel_values']}\n")


# ---------------------------------------------------------------------------
# Part 2 — IrreversibleMigrationError
# ---------------------------------------------------------------------------


def demo_irreversible() -> None:
    print("=" * 60)
    print("PART 2 — IrreversibleMigrationError on downgrade past c3e2")
    print("=" * 60, "\n")

    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()

    _seed(
        saver,
        "head-thread",
        {
            "topic": "Black holes",
            "sources": ["arxiv:0001"],
            "summary": "Dense objects.",
            "findings": [],
            "depth": 2,
            "sub_topics": [],
        },
        {REVISION_METADATA_KEY: engine.head()},
    )

    config = {"configurable": {"thread_id": "head-thread", "checkpoint_ns": ""}}
    raw = saver.get_tuple(config)
    from langmigrate.core.version import envelope_from_parts

    envelope = envelope_from_parts(raw.checkpoint["channel_values"], dict(raw.metadata or {}))

    print(f"Current revision: {envelope.revision!r}")
    print("Attempting downgrade to 'b2d1' (must cross the irreversible c3e2.downgrade)...")
    try:
        engine.downgrade_state(envelope, "b2d1")
        print("  [ERROR] Should have raised IrreversibleMigrationError!\n")
    except IrreversibleMigrationError as e:
        print(f"  Caught IrreversibleMigrationError: {e}\n")

    # Show a safe downgrade on a state that hasn't crossed the irreversible barrier.
    from langmigrate.core.version import envelope_from_parts

    env_b2d1 = envelope_from_parts(
        {
            "topic": "Exoplanets",
            "sources": [],
            "summary": "",
            "findings": [],
            "depth": 1,
            "sub_topics": [],
        },
        {REVISION_METADATA_KEY: "b2d1"},
    )
    print("Separate state at b2d1 (reversible migrations only below it):")
    print(f"  revision before downgrade: {env_b2d1.revision!r}")
    stepped = engine.downgrade_state(env_b2d1, "a1c0")
    print(f"  revision after downgrade to a1c0: {stepped.revision!r}")
    print(f"  findings removed: {'findings' not in stepped.values}\n")


# ---------------------------------------------------------------------------
# Part 3 — Staged upgrade (partial target)
# ---------------------------------------------------------------------------


def demo_staged_upgrade() -> None:
    print("=" * 60)
    print("PART 3 — Staged / targeted upgrade (stop at intermediate revision)")
    print("=" * 60, "\n")

    engine = MigrationEngine(MigrationRegistry.from_path(MIGRATIONS))
    saver = InMemorySaver()

    _seed(
        saver,
        "staged-thread",
        {"topic": "CRISPR", "sources": [], "summary": "", "debug_info": "old"},
        {},
    )

    lineage = engine.registry.lineage(engine.head())
    print("Migration history (oldest → newest):")
    for rev in lineage:
        m = engine.registry.get(rev)
        print(f"  {m.revision}  ({m.slug})")
    print()

    # Upgrade only to b2d1 (skip c3e2 for now)
    interceptor_partial = MigrationInterceptor(saver, engine, write_back=True, target="b2d1")
    migrated_partial = interceptor_partial.get_tuple(
        {"configurable": {"thread_id": "staged-thread", "checkpoint_ns": ""}}
    )
    partial_rev = (migrated_partial.metadata or {}).get(REVISION_METADATA_KEY)
    partial_values = migrated_partial.checkpoint["channel_values"]
    print(f"Partial upgrade (target=b2d1)  → revision: {partial_rev!r}")
    print(f"  debug_info still present: {'debug_info' in partial_values}")
    print(f"  findings present         : {'findings' in partial_values}\n")

    # Upgrade to HEAD
    interceptor_full = MigrationInterceptor(saver, engine, write_back=True)
    migrated_full = interceptor_full.get_tuple(
        {"configurable": {"thread_id": "staged-thread", "checkpoint_ns": ""}}
    )
    full_rev = (migrated_full.metadata or {}).get(REVISION_METADATA_KEY)
    full_values = migrated_full.checkpoint["channel_values"]
    print(f"Full upgrade   (target=HEAD)   → revision: {full_rev!r}")
    print(f"  debug_info removed      : {'debug_info' not in full_values}")
    print(f"  findings present        : {'findings' in full_values}\n")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    demo_node_remap()
    demo_irreversible()
    demo_staged_upgrade()


if __name__ == "__main__":
    main()
