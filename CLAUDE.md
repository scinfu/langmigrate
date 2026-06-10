# CLAUDE.md — LangMigrate

Declarative schema migrations for LangGraph state persistence (checkpointers & stores).
This file defines the conventions every contributor (human or AI) must follow.

## What this project is

When a LangGraph application evolves its state schema (`TypedDict` / Pydantic), old or
interrupted threads persisted by a checkpointer (Postgres, Redis, ...) stop deserializing
cleanly. LangMigrate brings the **Alembic model** to LangGraph state: declarative
revisions with a cascade of transformation functions, applied either **offline** (proactive
batch CLI) or **online** (lazy runtime interceptor).

## Architecture — Clean Architecture, strictly enforced

```
cli/  runtime/  adapters/   ──────►   core/
        (DB clients live here)        (pure: no DB client imports)
```

**Dependency rule:** `cli`, `runtime`, and `adapters` may import from `core`. `core` must
NEVER import a database client (`psycopg`, `redis`, ...) nor anything from `adapters` /
`runtime` / `cli`. Keep migration business logic independent of any backend.

- `core/` — pure logic: `types`, `exceptions`, `operations`, `migration`, `registry`,
  `engine`, `version`, `topology`. No I/O, no DB drivers.
- `adapters/` — DB-specific bulk access for the batch CLI. DB client imports are confined
  here, ideally imported lazily inside methods so the core stays importable without extras.
- `runtime/` — `MigrationInterceptor`, a `BaseCheckpointSaver` wrapper for lazy online
  migration. DB-agnostic: it delegates to whatever saver it wraps.
- `cli/` — Typer app.

## Core design decisions (do not change without discussion)

1. **Versioning = Alembic-style DAG.** Each revision has a `revision` hash and a
   `down_revision` pointer — a single id, or a **tuple of ids for a merge revision**
   (`langmigrate merge`). The engine resolves a path through the DAG (deterministic
   topological linearization of ancestor-set differences), then applies it as a linear
   cascade.
2. **Version tag lives ONLY in `checkpoint.metadata`** under the key `langmigrate_rev`.
   Never store it inside `channel_values` (it is metadata, not application state, and would
   risk being pruned by LangGraph). It must stay queryable at the DB level. **Stores are
   the one exception**: `Item.value` is the only persisted payload, so for store items the
   tag lives under the same reserved key *inside the value* — injected on put and stripped
   from every returned item by `MigrationStore`, so neither application code nor migrations
   ever observe it.
3. **Lazy write-back is ON by default, disableable, and idempotent.** Re-persisting a
   migrated checkpoint must NOT change `checkpoint["id"]` nor break the `parent_config`
   chain. Write-back happens only on `get_tuple`/`aget_tuple` (single checkpoint on
   resume). `list`/`alist` migrate **in memory only, never writing back** — they
   enumerate history (many checkpoints) and healing there would be a write storm and
   would rewrite past checkpoints. The same split applies to stores: `get`/`aget` heal,
   `search`/`asearch` migrate in memory only. The proactive "cure the DB" path is the
   batch runner (`langmigrate upgrade` / `langmigrate store upgrade`), not `list()`.
4. **Adapters:** Postgres and Redis are both implemented for checkpoints (batch
   enumeration + the shared online interceptor); Postgres also has a store adapter.
   `setup()` creates expression indexes (`ix_checkpoints_langmigrate_rev`,
   `ix_store_langmigrate_rev`) so the stale queries are indexed; Redis scans
   `checkpoint:*` RedisJSON docs (no server-side index on the tag). Postgres
   enumeration is keyset-paginated.
5. **Unknown stored revisions** (code rollback after a lazy migration) are governed by
   the `on_unknown_revision` policy (`"raise"` default, `"warn"`/`"pass"` serve the
   state unmigrated). The tolerance applies only to the state's own tag — bad targets
   and broken registry pointers always raise.

## Migration rules (binding)

- Every `upgrade` MUST have a corresponding `downgrade`. Genuinely irreversible migrations
  must be marked explicitly and raise `IrreversibleMigrationError` from `downgrade`.
- Migrations MUST be **idempotent** and **pure** — no hidden I/O, no network, no clocks.
  Re-applying a migration to already-migrated state must be a no-op.
- Never introduce a breaking change without a downgrade script.
- Use the declarative helpers (`add_field`, `drop_field`, `rename_field`, `coerce_field`,
  `require_field`) instead of hand-mutating dicts where possible.

## Commands

```bash
uv sync --extra dev --extra postgres --extra redis --extra langchain  # set up the environment
uv run pytest                              # unit tests
uv run pytest -m integration               # integration tests (needs Docker)
uv run ruff check . && uv run ruff format .  # lint + format
docker compose up -d                       # local Postgres + Redis for integration
```

## Code style

- Python 3.10+, full type hints, Pydantic v2.
- `ruff` for lint + format (config in `pyproject.toml`). Line length 100.
- Docstrings on all public APIs.
- No heavy dependencies in `core`.
- Public exports go through `langmigrate/__init__.py`.

## Testing

- Every operation/primitive has unit tests, including Safe vs Unsafe behavior.
- Engine tests must cover the cascade, idempotency, and the no-op-at-HEAD case.
- Each adapter has integration tests behind `@pytest.mark.integration` (requires Docker).
- Prefer the in-memory saver for runtime/interceptor unit tests.
