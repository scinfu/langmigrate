# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **State-level migration no longer loses type-only changes.**
  `migrate_state_update` (and therefore `SchemaMigrationMiddleware`) filtered the
  update with plain `!=`, so a coercion that only changes the type (`1` → `1.0`,
  `True` → `1`) was dropped from the update while the state was still stamped
  with the new revision — silently losing the migration. The update diff now
  uses the same strict deep-equality as the checkpoint write-back path.
- **Empty registry no longer breaks the wrapped saver/store.** With no revisions
  yet (e.g. right after `langmigrate init`), `MigrationInterceptor` raised
  `MultipleHeadsError` on every `put` (and on reads of existing checkpoints)
  while resolving the head. `MigrationInterceptor`, `MigrationStore` and
  `migrate_state_update` are now transparent pass-throughs when the registry is
  empty: nothing is stamped, nothing is migrated.
- **`SchemaMigrationMiddleware` honors a custom `rev_key`.** The contributed
  `state_schema` always declared the default `langmigrate_rev` channel, so a
  custom `rev_key` produced updates to an undeclared channel (rejected by
  LangGraph). The middleware now declares the channel matching the configured
  key.
- **`rename_field` treats a type-only collision as a conflict.** When both keys
  existed with values that compare `==` but differ in type at any depth
  (`1` vs `1.0`), the target value was silently overwritten; it now raises
  `UnsafeMigrationError`, consistent with the strict-equality semantics used
  everywhere else.
- **`PostgresStoreAdapter.stamp_all` guards against JSON `null` values.**
  `jsonb_set` of a null base returns null and would silently drop the tag; the
  store stamp now uses the same `COALESCE(NULLIF(...))` guard as the checkpoint
  adapter.

## [1.1.1] — 2026-06-11

Documentation and packaging metadata only — no code changes.

### Changed

- **README "Symptoms" section** now also covers stale `BaseStore` items
  (cross-thread memory persisted under an old value shape), with the real
  traceback, and links the runnable examples.
- **New "Runnable examples" section** in the README pointing at `examples/`,
  starting with the LangGraph Studio break-and-heal walkthrough.
- **Expanded PyPI keywords** (`langchain`, `schema-migration`, `checkpointer`,
  `store`, `memory`, `persistence`, `agent`, `pydantic`, `postgres`, `redis`, ...)
  for discoverability.

## [1.1.0] — 2026-06-10

### Added

- **Merge revisions (multi-parent DAG).** `down_revision` may now be a tuple of
  parent ids; `langmigrate merge -m "..." [rev1 rev2 | heads]` scaffolds a merge
  revision joining branched heads. Path resolution is a deterministic topological
  linearization (ancestor-set differences, ties broken on revision id), so linear
  histories behave exactly as before. `history` renders merge parents as `a + b`.
- **LangGraph store support.** `MigrationStore` wraps any `BaseStore` and migrates
  item values lazily; the revision tag lives under the reserved `langmigrate_rev`
  key *inside* `Item.value` (injected on put, stripped from every returned item).
  New `setup_langmigrate_store` factory, `PostgresStoreAdapter`,
  `run_store_batch_upgrade` / `run_store_batch_downgrade`, and a
  `langmigrate store {revision,history,current,check,upgrade,downgrade,stamp}`
  CLI sub-app (`init --with-store` scaffolds `store_migrations/`). Redis store
  batch enumeration is deferred; the online wrapper is backend-agnostic.
- **Rollback safety: `on_unknown_revision` policy** (`"raise"` | `"warn"` |
  `"pass"`) on `MigrationInterceptor`, `MigrationStore` and the factories. With
  `"warn"`/`"pass"`, state tagged with a revision missing from the registry (a
  code rollback after lazy migration) is served unmigrated instead of failing
  the read. Default stays `"raise"`.
- **Batch error tolerance.** `continue_on_error` on every batch runner records
  per-checkpoint/item `BatchFailure`s (`ref`, `error`, `error_type`) instead of
  aborting; `BatchResult` gains `failed` / `failures` / `ok`; the CLI grows
  `--continue-on-error` and exits non-zero listing failures.
- **Async batch path.** `arun_batch_upgrade` / `arun_batch_downgrade` plus
  `AsyncPostgresAdapter` (psycopg `AsyncConnection` + `AsyncPostgresSaver`) for
  running proactive migration inside async services. The CLI stays sync.
- **Postgres expression indexes** on `metadata->>'langmigrate_rev'` (checkpoints)
  and `value->>'langmigrate_rev'` (store), created by `setup()` — the stale scan
  is now actually indexed, as documented.
- `MigrationInterceptor.delete_thread` / `adelete_thread` now delegate to the
  wrapped saver.

### Changed

- **Dry-run now validates.** `upgrade --online-dry-run` / `downgrade --dry-run`
  execute the full cascade in memory (surfacing migration bugs against real
  data) and only skip the write; previously stale checkpoints were just counted.
- **Strict deep-equality for write-back.** A type-only change nested inside a
  container (e.g. `1` → `1.0` in a dict) is now detected by `coerce_field` and
  the version reconciliation, so the migrated blob is actually written back.
- Postgres enumeration uses keyset pagination (no more full-result
  materialization); the batch runners enumerate once (no `count_stale`
  pre-pass — Redis no longer pays a double scan).
- `MigrationRegistry.lineage()` is redefined as "all ancestors in topological
  order" (identical output for pre-1.1 linear histories). `heads()` and ancestor
  sets are cached (the registry is immutable after construction).
- On downgrade the engine stamps the final target once at the end (identical
  result for linear histories; well-defined across merges).
- CLI renders registry errors (duplicates, cycles, unknown parents) as messages
  instead of tracebacks.

### Removed

- `FieldOp`, `OpKind`, `SAFE_OPS` from `langmigrate.core.types` (never exported
  from the package, no references anywhere).

### Known limitations

- `pending_writes` are passed through unmigrated (single-channel fragments; see
  the note in `langmigrate.runtime.interceptor`). Run `langmigrate upgrade`
  before deploys that change channels written by interrupted tasks.

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


