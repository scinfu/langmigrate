# Example: multi_tool_agent

A realistic **tool-calling ReAct agent** (planner → tool → responder) built with
LangGraph's `StateGraph`. Three legacy threads at different schema versions are seeded
into an `InMemorySaver`, then resumed through a `MigrationInterceptor` — the agent code
itself is unchanged.

## Schema evolution

| Revision | Change | Safety |
|---|---|---|
| `a1c0` (`add_session`) | add `session_id` (UUID), `iteration` int | Safe |
| `b2d1` (`add_tool_count`) | rename `user_input → query`, add `tool_calls_count` | Unsafe |
| `c3e2` (`require_query`) | require `query` present, coerce `iteration` to int | Unsafe |

## Seeded threads

| Thread | Schema | Quirk |
|---|---|---|
| `thread-v0` | v0 | Uses old `user_input` key |
| `thread-v1` | a1c0 | `iteration` stored as a string `"2"` |
| `thread-v2` | b2d1 | Already renamed, but not yet at head |

## Run it

```bash
uv run python examples/multi_tool_agent/demo.py
```

For each thread you'll see:
- The raw revision stored in the DB
- The fields after lazy migration through the interceptor
- The graph output after resuming (planner produces a plan, tool runs, responder answers)

## Key takeaways

- `MigrationInterceptor` wraps any `BaseCheckpointSaver` — zero changes to graph code.
- Rename (`user_input → query`) is handled safely: old threads arrive at the planner
  with `query` populated.
- Type coercion (`iteration` as `str → int`) is idempotent: re-running the migration
  on an already-coerced value is a no-op.
- Write-back keeps checkpoint IDs stable — the `parent_config` chain is intact.

## Inspect with the CLI

```bash
cd examples/multi_tool_agent
uv run langmigrate history
uv run langmigrate check
```
