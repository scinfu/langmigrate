# Example: middleware_agent

**Use case:** You deploy on **LangGraph Server / Cloud** (or any managed platform) and
**cannot** wrap the checkpointer directly. Instead you add a lightweight `migrate` node
at the graph entry point, or drop in `SchemaMigrationMiddleware` if your agent supports
the middleware stack.

## When to use this pattern

| Pattern | Own the saver? | Remove channels? | Overhead |
|---|---|---|---|
| `MigrationInterceptor` | Yes | Yes (full rebuild) | None on write |
| `migrate_state_update` node | No | No (merges only) | One node per run |
| `SchemaMigrationMiddleware` | No | No | Before model/agent hooks |

Choose this pattern when you use LangGraph Server/Cloud, or when adding
`MigrationInterceptor` would require restructuring a large shared codebase.

## Schema evolution

| Revision | Change | Safety |
|---|---|---|
| `a1c0` (`add_metadata`) | add `metadata` dict with default `{}` | Safe |
| `b2d1` (`add_confidence_and_model`) | add `confidence_score: float = 0.0` and `model_id: str = "gpt-4"` | Safe |

## Run it

```bash
uv run python examples/middleware_agent/demo.py
```

The demo has three parts:

1. **`migrate_state_update`** — the bare primitive: feeds a v0 dict and shows the
   returned update dict (only new/changed fields + the revision tag).
2. **Node pattern** — wires the same call into a real `StateGraph` so migrations run
   transparently on every invocation.
3. **`SchemaMigrationMiddleware`** — shows how the middleware wraps the same logic
   and the channel-removal limitation.

## Key limitation

`migrate_state_update` returns a *state update* (merged in, never replaces). A
migration that **renames** (`msgs → messages`) or **drops** a field cannot remove the
old channel — LangGraph just adds the new one on top. Use `MigrationInterceptor` (the
`evolving_agent` example) when you need hard channel removal.

## Inspect with the CLI

```bash
cd examples/middleware_agent
uv run langmigrate history
uv run langmigrate check
```
