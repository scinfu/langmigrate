# Example: batch_migration

**Proactive offline migration** — run before a release to upgrade (or downgrade) every
checkpoint in the database at once, rather than waiting for threads to be loaded lazily.

## When to use this pattern

| Pattern | When to use |
|---|---|
| `MigrationInterceptor` | Gradual: threads migrate on first resume |
| `run_batch_upgrade` | Before a release: cure the DB in one go, off-peak |
| `run_batch_downgrade` | Rollback: revert all checkpoints to a previous revision |

The batch path is especially important for **breaking changes** where you can't afford
to let any thread reach the new code in the old schema.

## Schema evolution

| Revision | Change | Safety |
|---|---|---|
| `a1c0` (`add_priority_and_tags`) | add `priority: str = "normal"`, `tags: list = []` | Safe |
| `b2d1` (`normalise_status`) | coerce `status` to lowercase, add `attempt: int = 1` | Unsafe coerce |

## Run it

```bash
uv run python examples/batch_migration/demo.py
```

The demo:
1. Seeds 5 threads at v0, v1, and v2 schema
2. Runs a **dry-run** (counts stale, zero writes)
3. Runs a real **batch upgrade to HEAD**
4. Shows all threads at head, `status` normalised
5. Runs a **batch downgrade to `a1c0`** (partial rollback)
6. Runs a **downgrade to base** (removes all revisions)

## Real-world: Postgres

Replace `InMemoryAdapter` with `PostgresAdapter`:

```python
from langmigrate.adapters.postgres import PostgresAdapter

adapter = PostgresAdapter.from_conn_string(
    "postgresql://user:pass@localhost:5432/mydb"
)
adapter.setup()  # ensure checkpoint tables exist
result = run_batch_upgrade(adapter, engine)
print(result)
```

Or use the CLI directly:

```bash
export LANGMIGRATE_URL="postgresql://user:pass@localhost:5432/mydb"
uv run langmigrate upgrade head --dry-run  # preview first
uv run langmigrate upgrade head            # apply
```

## Inspect with the CLI

```bash
cd examples/batch_migration
uv run langmigrate history
uv run langmigrate check
```
