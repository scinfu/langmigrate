# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The batch runners now honour an `on_unknown_revision` policy, so a single
  checkpoint/item tagged with a revision absent from the registry no longer
  aborts the whole run.** The lazy paths (`MigrationInterceptor`,
  `MigrationStore`, `migrate_state_update`) already tolerate a state whose own
  tag the registry does not know — the documented "code rollback after a lazy
  migration" case — but `run_batch_upgrade` / `run_batch_downgrade` (plus their
  async and store twins) had no such option: `upgrade_state` raised
  `RevisionNotFoundError` and aborted the entire run (or, with
  `--continue-on-error`, recorded every such item as a *failure* rather than a
  skip). All six runners now accept `on_unknown_revision` (`"raise"` default
  keeps the old behaviour; `"warn"`/`"pass"` skip the item — counted in `total`
  but not `migrated`). The tolerance applies only to the item's *own* tag; a bad
  target or a broken registry pointer still raises. Exposed on the CLI as
  `--on-unknown-revision` for `upgrade`, `downgrade`, `store upgrade` and
  `store downgrade`.
- **`NodeRemap` now validates a rename's target against `known_nodes`.** A
  rename pointing at a node that is itself gone (a stale remap) silently
  redirected the thread onto a non-existent node — just moving the deadlock.
  When `known_nodes` is supplied, an unknown rename target now raises a
  structured `TopologyMismatchError`. Behaviour without `known_nodes` is
  unchanged.
- **`SchemaMigrationMiddleware` now forwards `on_reserved_key_collision`.** The
  middleware never passed the policy through to `migrate_state_update`, so it
  was stuck on the `"warn"` default and `"error"` could not be selected on that
  path.
- **`revision --autogenerate` after a merge now diffs against both branches.**
  The autogenerate baseline (`_baseline_fields`) walked the lineage and returned
  the *first* `fields` snapshot it found, so for a merge — whose own snapshot is
  `None` — it picked one parent branch arbitrarily. The new code schema was then
  diffed against only that branch: fields living solely on the other branch were
  reported as spurious additions, and — worse — a field the new schema *drops*
  or *retypes* would be missed entirely if it came from the unpicked branch,
  silently leaving it out of the generated migration. The baseline is now
  reconstructed as the **union of the schemas after each parent**, so a
  post-merge autogenerate sees the full pre-merge schema. Linear histories are
  unaffected (a revision's own snapshot, or the nearest ancestor's, is used).

## [1.2.2] — 2026-06-14

### Fixed

- **`langmigrate stamp` / `langmigrate store stamp` now render an unknown
  revision as a clean error instead of a raw traceback.** The revision-exists
  validation called `registry.get(revision)` directly, so passing a revision id
  not in the registry let `RevisionNotFoundError` escape and Typer dumped a full
  traceback (still exit code 1, but inconsistent with every other CLI command,
  which renders `LangMigrateError` as a single red line). Both `stamp` commands
  now go through a shared `_require_revision` helper that prints the message and
  exits 1 cleanly. No behavioural change beyond the error presentation.

## [1.2.1] — 2026-06-13

### Fixed

- **`upgrade_state` to a target the state is already past is now a no-op
  instead of a crash.** `MigrationEngine.upgrade_state` to a pinned older
  `target` used to raise `RevisionNotAncestorError` when the state was already
  written *ahead* of that target (e.g. a mixed-version deploy or a partial
  rollback where some threads were lazily migrated past it) — crashing the read
  in `MigrationInterceptor` / `MigrationStore` / `migrate_state_update`. It now
  returns the state unchanged when the target is an ancestor of the state's
  revision. Genuinely divergent revisions (neither ancestor nor descendant of
  the target) still raise. Downgrade is intentionally left strict:
  `downgrade_state` (and the `langmigrate downgrade` CLI) still surface
  "downgrade to a higher revision" as a clear `RevisionNotAncestorError` rather
  than silently doing nothing. The low-level `MigrationRegistry.upgrade_path` /
  `downgrade_path` are unchanged.
- **`revision --autogenerate` no longer emits an unparseable migration for a
  non-builtin type change.** When a field's type changed to anything outside
  `{int, str, float, bool}` (e.g. `list[int]`, `dict[str, int]`, a custom
  class), the generated `coerce_field` call inlined the TODO placeholder as
  `lambda v: v  # TODO ...` *inside* the call — so the `#` commented out the
  closing `)` and the file failed to import with
  `SyntaxError: '(' was never closed`. Because discovery imports every file in
  the directory, a single such revision broke the *entire* migrations
  directory (`history`, `check`, `upgrade`, `MigrationRegistry.from_path` all
  failed). The TODO is now emitted on its own comment line above the statement
  and the coercion expression stays a clean, valid `lambda v: v`.
  `_coercion_expr` now returns `(expr, todo_comment)` instead of a single
  string.
- **Fluent `state.require_field(...)` no longer reports the wrong revision.**
  The fluent helper on `StateEnvelope` passed `revision=self.revision` to
  `MissingRequiredFieldError` — but on an envelope `self.revision` is the
  state's *current/source* tag (the revision being migrated *from*), not the
  migration that requires the field. During the cascade the envelope is
  stamped with the new revision only *after* `upgrade()` returns, so a field
  required by `v2` was reported as missing "(revision v1)", pointing an
  operator at the wrong migration. The fluent helper has no handle on the
  migration being applied, so it now passes `revision=None` (omitting the
  misleading detail) while `BaseMigration.require_field(state, ...)` continues
  to supply the accurate migration revision. Behaviour on the data is
  unchanged; only the diagnostic was wrong.

### Internal

- **Docker-free unit coverage for the adapter pure logic** (`tests/unit/
  test_adapters_logic.py`). The Postgres/Redis keyset SQL + parameter
  construction, store namespace round-trip, `<untagged>` aggregation, and the
  Redis metadata parsing were previously exercised only by the integration
  suite (which needs Docker). They now have a fast regression net using fake
  DB clients, so a change to the query shape is caught without a database.

## [1.2.0] — 2026-06-12

### Added

- **`on_unknown_revision` policy on the state/middleware path.**
  `migrate_state_update` and `SchemaMigrationMiddleware` now accept the same
  `"raise"` | `"warn"` | `"pass"` policy as `MigrationInterceptor` /
  `MigrationStore`, so a code rollback after a lazy migration no longer has to
  crash the agent — with `"warn"`/`"pass"` the state is served unmigrated. As in
  the interceptor, the tolerance covers only the state's own tag; a bad `target`
  still raises. Default stays `"raise"`. The `OnUnknownRevision` type now lives
  in `langmigrate.core.types` (still re-exported from the package root and
  `langmigrate.runtime.interceptor`).

### Fixed

- **`MigrationStore` no longer crashes on a stored `value=None`.** LangGraph's
  own stores never return an `Item` with `value=None` (`PutOp(value=None)`
  means delete), but external or custom `BaseStore` implementations can — and
  the wrapper used to crash on read because `strip_value_tag` blindly did
  `dict(value)`. `strip_value_tag(None)` now returns `{}`,
  `MigrationStore._migrate_item` skips the cascade for `None` values,
  `rebuild_item` preserves the `None` in the returned `Item`, and
  `run_store_batch_upgrade` / `run_store_batch_downgrade` skip `None`-valued
  items instead of crashing.
- **`MigrationRegistry` rejects non-string revision ids at load time.** A
  typo like `revision = 42` was silently accepted (the `if not m.revision`
  check passes for non-zero ints); the engine then compared `42` against
  string-typed registered revisions and `read_revision` treated any int tag
  as untagged — the symptom was a "checkpoint looks untagged" with no link
  to the misconfigured migration. Both `MigrationRegistry.__init__` and
  `FunctionMigration.__init__` now raise an explicit `TypeError` for
  non-string revisions.
- **Redundant-merge parents are rejected by the registry, not just the CLI.**
  The CLI's `langmigrate merge` refused to create a merge whose parents were
  in an ancestor/descendant relationship, but hand-written merges bypassed
  the check. The registry now enforces the same invariant in `_validate`,
  raising the new `InvalidMigrationGraphError` (re-exported from the package
  root) with a message naming the redundant parent and its descendant. Note
  this is a consistency/hygiene check, not a correctness fix: the resolved
  cascade was already identical with or without the redundant edge (the
  ancestor-set difference and topological sort ignore it). Registries that
  previously loaded with such a merge will now fail at construction time —
  drop the redundant parent from `down_revision` to fix.
- **Reserved-key collision on `langmigrate_rev` is now surfaced.** Both
  `MigrationStore` and `migrate_state_update` silently overwrote a user
  value under the reserved `langmigrate_rev` key on every write, with no
  signal to the application. A new `on_reserved_key_collision` policy
  (`"warn"` | `"error"`, default `"warn"`) is exposed on `MigrationStore`
  and `migrate_state_update`, propagated through
  `setup_langmigrate_store` (the new `OnReservedKeyCollision` literal lives
  in `langmigrate.core.types`; `ReservedKeyCollisionError` lives in
  `langmigrate.core.exceptions` and is re-exported from the package root).
- **Self-loop error message is no longer a duplicated node.** A migration
  whose `down_revision` pointed to itself used to report
  `Cycle detected in migration history involving: self, self`. The check
  now detects the self-loop explicitly and emits
  `Cycle detected in migration history involving: self (self-loop)`, which
  is unambiguous to read and won't trip log parsers that match the path.
- **Concurrency caveat of `MigrationInterceptor` write-back is documented.**
  The `get_tuple` / `aget_tuple` write-back is a read-modify-write under
  the wrapped saver's own locking, not an atomic compare-and-set: a
  concurrent `put` on the same thread between the read and the write can
  be silently overwritten. The module docstring and the method docstrings
  now call out the single-writer-per-thread requirement and the
  deployment patterns that need to disable write-back or serialize access.
- **`PostgresStoreAdapter` namespace roundtrip is documented.** LangGraph
  stores namespaces dot-joined and the adapter splits on `.` to recover the
  tuple. The docstrings now spell out why that is safe: the LangGraph API
  rejects `.` inside a namespace label (`InvalidNamespaceError`), so the
  split is unambiguous for anything written through it — only rows inserted
  into the `store` table outside the API could produce an ambiguous prefix.
- **`stamp_value` / `stamp_metadata` docstrings declare the reserved key.**
  The fact that `langmigrate_rev` is reserved is now stated in the helper
  docstrings (it was previously only mentioned in the module-level notes),
  pointing at the new `on_reserved_key_collision` policy for users who
  need the wrapper to detect the collision.
- **CI type check no longer fails on `SchemaMigrationMiddleware`'s dynamic
  `state_schema`.** Recent mypy releases attribute the "Invalid TypedDict()
  field name" error of the functional `TypedDict(...)` call (whose field name is
  the runtime `rev_key`) to the line carrying the field dict, so the existing
  `# type: ignore[misc]` on the call's opening line stopped suppressing it and
  `mypy src/langmigrate` failed. The suppression now sits on the reported line.
- **Batch downgrade counts scanned checkpoints/items consistently.** The
  downgrade runners (`run_batch_downgrade`, `arun_batch_downgrade`,
  `run_store_batch_downgrade`) incremented `total` only after a successful
  fetch, so with `--continue-on-error` a checkpoint/item failing inside the
  fetch was recorded among the failures without being counted — the summary
  could report `total < migrated + failed`. `total` now counts every scanned
  entry, matching the upgrade runners.
- **Write-back no longer drops `channel_versions` of ephemeral channels.**
  Real LangGraph checkpoints carry versions for channels that have no loaded
  value (`__start__`, `branch:to:*` — consumed and empty at that checkpoint).
  The version reconciliation rebuilt `channel_versions` from `channel_values`
  only, so the lazy write-back (and the batch runners) silently removed those
  entries while `versions_seen` still referenced them — rewriting the checkpoint
  into a shape LangGraph never produced. Only channels actually dropped by a
  migration are removed now; version-only channels keep their versions.
- **`SchemaMigrationMiddleware` is now a stable class object.** The module-level
  `__getattr__` (PEP 562) rebuilt the class on every attribute access, so two
  imports yielded *different* classes and `isinstance` checks across them failed
  silently. The class is built once and cached in the module namespace.
- **Non-string revision tags no longer crash the state path.**
  `migrate_state_update` raised a Pydantic `ValidationError` when the state's
  `langmigrate_rev` value was not a string (e.g. corrupted state); it now treats
  a non-string tag as untagged, mirroring how the checkpoint path reads
  `checkpoint.metadata`.
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


