# Example: deep_research_agent

A **supervisor-style deep research agent** (planner → web_researcher → synthesizer →
reviewer) that demonstrates three advanced LangMigrate features unavailable in simpler
examples:

## Features demonstrated

### 1. NodeRemap — topology migration

Between v1 and v2 the graph node `research_step` was renamed to `web_researcher`.
Threads interrupted mid-run on the old node would deadlock on resume. The `b2d1`
migration calls `remap_node` to repair the stored node reference:

```python
state = self.remap_node(
    state,
    renames={"research_step": "web_researcher"},
    fallback="planner",
)
```

Any thread paused on an unknown node falls back to `planner` rather than crashing.

### 2. IrreversibleMigrationError

The `c3e2` migration drops `debug_info` permanently. Its `downgrade` calls
`self.raise_irreversible()`. Attempting to downgrade past it raises
`IrreversibleMigrationError` — an explicit, structured signal rather than silent
data loss.

### 3. Staged / partial upgrade targeting

`MigrationInterceptor(..., target="b2d1")` stops the cascade at an intermediate
revision — useful for gradual rollouts, canary deployments, or A/B experiments where
two graph versions run in parallel.

## Schema evolution

| Revision | Change | Safety |
|---|---|---|
| `a1c0` (`add_depth_and_subtopics`) | add `depth: int = 1`, `sub_topics: list = []` | Safe |
| `b2d1` (`add_findings_and_remap_node`) | add `findings: list`, remap `research_step → web_researcher` | Safe + Topology |
| `c3e2` (`drop_debug_info`) | drop `debug_info` (irreversible) | Safe drop, irreversible downgrade |

## Run it

```bash
uv run python examples/deep_research_agent/demo.py
```

## Inspect with the CLI

```bash
cd examples/deep_research_agent
uv run langmigrate history
uv run langmigrate check
```
