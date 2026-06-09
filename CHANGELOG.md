# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`setup_langmigrate(saver, migrations)` factory.** One-liner that builds the
  registry, engine and `MigrationInterceptor` for you. Accepts a path, a
  `MigrationRegistry`, or a `MigrationEngine`; forwards `write_back` / `target`.
- **`@migration` decorator and `FunctionMigration`.** Write inline function-pair
  migrations without subclassing `BaseMigration`; attach the reverse with
  `.reverse`. `MigrationRegistry.from_path` now discovers both decorator-built
  instances and `BaseMigration` subclasses.
- **Fluent `StateEnvelope` helpers.** `state.add_field(...)`, `.drop_field(...)`,
  `.rename_field(...)`, `.coerce_field(...)`, `.require_field(...)` — the same
  pure operations as methods, for the function-pair style.
- **`BaseMigration.is_reversible`.** Used by `langmigrate check` to flag one-way
  migrations (works for both authoring styles).
- **`langmigrate init` scaffolding.** Now also writes `migrations/__init__.py`
  and a `migrations/README.md`; `--example` drops a first revision skeleton.
- **Quickstart example** (`examples/quickstart/`) type-checked with `mypy --strict`.
- Public exports: `setup_langmigrate`, `migration`, `FunctionMigration`,
  `new_revision_id`.

## [1.0.0] — 2026-06-05

First stable release. LangMigrate brings declarative, Alembic-style schema
migrations to LangGraph state persistence — checkpointers (Postgres, Redis)
and stores.

### Added

- **Migration engine and registry.** Alembic-style DAG with `revision` /
  `down_revision`, path resolution, cycle and multiple-head detection.
- **Pure, idempotent operations.** `add_field`, `drop_field`, `rename_field`,
  `coerce_field`, `require_field` — Safe vs Unsafe annotated, with
  `IrreversibleMigrationError` for genuinely one-way migrations.
- **Declarative migrations.** `BaseMigration` ABC with helpers that delegate
  to the pure operations module.
- **Topology repair.** `NodeRemap` helper for interrupted threads on
  deleted/renamed graph nodes, applicable within any migration.
- **CLI (`langmigrate`).** `init`, `revision` (with `--autogenerate --schema`
  for state-aware scaffolding), `history`, `current`, `check`, `upgrade`,
  `downgrade`, `stamp`.
- **Online migration.** `MigrationInterceptor` — drop-in
  `BaseCheckpointSaver` wrapper. Lazy upgrade on `get_tuple`/`aget_tuple`
  with idempotent write-back; `list`/`alist` migrate in-memory only to
  prevent write storms. `put`/`aput` stamp the HEAD revision.
- **Batch migration.** `run_batch_upgrade` / `run_batch_downgrade` for
  proactive cure of every stored checkpoint.
- **State-level middleware.** `SchemaMigrationMiddleware` for managed
  platforms where the checkpointer is owned by the framework
  (LangGraph Server, deepagents). Hooks `before_agent`, `before_model` and
  their async counterparts.
- **Pure helper.** `migrate_state_update` for hand-built `StateGraph`s or
  custom entry nodes.
- **Adapters.** `PostgresAdapter` (indexed `metadata->>'langmigrate_rev'`
  filter) and `RedisAdapter` (scan-based RedisJSON enumeration).
- **Version tag.** Stored in `checkpoint.metadata`, never in
  `channel_values` — queryable at the DB level and safe from pruning.
- **Schema autogenerate.** `revision --autogenerate --schema <module>:<class>`
  diffs the current state schema against the last revision and scaffolds
  the migration body.
- **Test suite.** 114 unit tests + 8 integration tests covering Postgres
  and Redis end-to-end, sync and async paths.

### Compatibility

- Python 3.10+
- Pydantic v2
- `langgraph-checkpoint` ≥ 2.0
- Optional: `psycopg` ≥ 3.1 (Postgres), `langgraph-checkpoint-redis` (Redis),
  `langchain` ≥ 1.0 (state-level middleware)


