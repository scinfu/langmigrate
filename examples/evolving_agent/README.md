# Example: evolving_agent

A minimal end-to-end demo of LangMigrate's **lazy online migration**, using
LangGraph's `InMemorySaver` so it runs with **no database**.

## The schema evolution

| Revision | Change | Safety |
|---|---|---|
| `a1c0` (`add_context`) | add `context` dict with a default | Safe |
| `b2d1` (`rename_msgs`) | rename `msgs` → `messages`, coerce `count` str → int | Unsafe (handled) |

A "v0" thread persisted before any of this has `{"msgs": [...], "count": "3"}` and
no revision tag.

## Run it

```bash
uv run python examples/evolving_agent/demo.py
```

You'll see a legacy v0 checkpoint loaded through a `MigrationInterceptor`, upgraded
through the cascade `v0 → a1c0 → b2d1`, and the database **self-healed** by the
idempotent write-back (same checkpoint id preserved).

## Inspect with the CLI

```bash
cd examples/evolving_agent
uv run langmigrate history
uv run langmigrate check
uv run langmigrate current
```

## Try it on Postgres

```bash
docker compose up -d          # from the repo root
export LANGMIGRATE_URL="postgresql://langmigrate:langmigrate@localhost:5442/langmigrate"
# wire a PostgresSaver into your graph, wrap it with MigrationInterceptor,
# or run a proactive batch:  uv run langmigrate upgrade head
```
