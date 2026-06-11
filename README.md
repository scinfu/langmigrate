# LangMigrate

> Declarative schema migrations for LangGraph state persistence — **Alembic for your
> checkpointers and stores**.

[![PyPI](https://img.shields.io/pypi/v/langmigrate)](https://pypi.org/project/langmigrate/)
[![Python versions](https://img.shields.io/pypi/pyversions/langmigrate)](https://pypi.org/project/langmigrate/)
[![CI](https://github.com/scinfu/langmigrate/actions/workflows/ci.yml/badge.svg)](https://github.com/scinfu/langmigrate/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/langmigrate)](./LICENSE)
[![Docs](https://img.shields.io/badge/docs-scinfu.github.io-blue)](https://scinfu.github.io/langmigrate/)

LangGraph persists application state through *checkpointers* (Postgres, Redis, ...) so graphs
can pause, resume, and survive failures. But as your app evolves, the state schema
(`TypedDict` / Pydantic) changes — fields get added, removed, renamed, retyped. Old or
interrupted threads resumed on newer code then fail to deserialize or silently corrupt data.

**LangMigrate** fixes this with declarative, versioned migrations applied either:

- **Proactively (batch)** — an offline CLI that walks every checkpoint in the database and
  upgrades it, or
- **Lazily (online)** — a runtime interceptor that upgrades a thread on the fly the moment it
  is loaded, via a cascade of transformation functions.

```python
from langmigrate import setup_langmigrate

saver = setup_langmigrate(base_saver, "migrations")  # that's it — pass to your graph
```

## Symptoms — do you need this?

You probably landed here after changing a LangGraph state schema and seeing an old or
interrupted thread blow up on resume. If any of these look familiar, LangMigrate is for you:

- **`pydantic_core._pydantic_core.ValidationError: 1 validation error for AgentState`** —
  `Field required [type=missing, ...]` when a checkpoint saved before you added a required
  field is loaded back into the new schema. The real traceback looks like this:

  ```text
    File ".../langgraph/pregel/_algo.py", line 1386, in _proc_input
      val = proc.mapper(val)
    File ".../langgraph/graph/state.py", line 1732, in _coerce_state
      return schema(**input)
    File ".../pydantic/main.py", line 263, in __init__
      validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
  pydantic_core._pydantic_core.ValidationError: 1 validation error for AgentState
  user_id
    Field required [type=missing, input_value={'messages': ['resume me']}, input_type=dict]
      For further information visit https://errors.pydantic.dev/2.13/v/missing
  Before task with name 'respond' and path '('__pregel_pull', 'respond')'
  ```

  LangGraph rebuilds your Pydantic state from the persisted channels (`_coerce_state ->
  schema(**input)`); a field added after the checkpoint was written is simply absent, so
  validation fails on resume.
- **`KeyError: '<field>'`** raised inside a node that reads a field which was *renamed* or
  *removed*, on a thread persisted under the old schema. With a `TypedDict` state and a
  renamed field, the resume fails right inside your node:

  ```text
    File ".../langgraph/pregel/_retry.py", line 617, in run_with_retry
      return task.proc.invoke(task.input, config)
    File ".../langgraph/_internal/_runnable.py", line 426, in invoke
      ret = self.func(*args, **kwargs)
    File "my_app/nodes.py", line 11, in respond
      last = state["messages"][-1]
  KeyError: 'messages'
  During task with name 'respond' and id '20014471-d5c7-1d58-2709-466e4bba78c2'
  ```

  The old thread persisted the field under its previous name (`msgs`), so `state["messages"]`
  isn't there on resume.
- **`langgraph.errors.InvalidUpdateError`** / **`EmptyChannelError`** after a channel
  (state key) changed shape or type between deploys.
- **Old checkpoints fail to deserialize** with `JsonPlusSerializer` / msgpack after a
  `TypedDict` or Pydantic state model changed (added, dropped, renamed, or retyped fields).
- **Long-term memory items (`BaseStore`) break too** — `KeyError` / `TypeError` inside a
  node reading a cross-thread memory item (`store.get(...)` / `store.search(...)`) whose
  value was saved under an old shape (e.g. flat `{"name": ...}` where the new code expects
  nested `{"profile": {...}}`):

  ```text
    File ".../langgraph/pregel/_retry.py", line 617, in run_with_retry
      return task.proc.invoke(task.input, config)
    File ".../langgraph/_internal/_runnable.py", line 426, in invoke
      ret = self.func(*args, **kwargs)
    File "my_app/nodes.py", line 14, in respond
      name = item.value["profile"]["name"]
  KeyError: 'profile'
  During task with name 'respond' and id '56c4b765-6d5c-021a-5351-ede94b08ecb2'
  ```

  Store items outlive any single thread, so one schema change breaks **every** thread that
  reads the shared item — including brand-new ones, which makes it look like a random
  regression rather than a persistence problem. Checkpoint fixes don't help here;
  LangMigrate's `MigrationStore` wrapper migrates items on read (and heals them in place
  on `get()`).
- **Resuming an interrupted thread after a graph refactor silently loses work** — the
  scariest variant, because there is *no* exception. A thread paused mid-node (e.g. on a
  human-in-the-loop `interrupt()`) is resumed on code where that node was renamed or removed;
  LangGraph can't reattach the pending task, so the in-flight decision is dropped and the
  resumed run returns stale state. No stack trace, no log line — just `langgraph interrupt
  resume not working` / silent state corruption after a deploy (topology drift).
- **"It worked before the deploy"** — Postgres/Redis checkpointer threads created on an
  older schema crash, silently lose data, or corrupt state on the new code.

These are all the same root cause: a LangGraph **checkpointer or store persisted state under
an old schema**, and your new code can't read it. LangMigrate versions and migrates that
state the way Alembic does for SQL — see below. Every symptom above is reproducible (and
fixable) hands-on in the [runnable examples](#runnable-examples).

## How it works

LangMigrate borrows the model that solved this exact problem for SQL databases — Alembic:

1. **Revisions.** Every schema change is a small, pure, idempotent function pair
   (`upgrade` / `downgrade`) identified by a revision id and chained through
   `down_revision` — a DAG that supports branching and merge revisions.
2. **A version tag on the persisted state.** Each checkpoint carries its revision id in
   `checkpoint.metadata["langmigrate_rev"]` — metadata, never application state, and
   queryable at the DB level (`setup()` creates an expression index for it). Store items
   carry the same tag inside `Item.value`, injected on write and stripped from every read,
   so neither your code nor your migrations ever see it.
3. **An engine that closes the gap.** When stored tag ≠ code head, the engine resolves a
   path through the DAG (deterministic topological linearization) and applies the upgrade
   cascade.

That engine runs through two delivery paths — use either or both:

```text
ONLINE (lazy)                                      OFFLINE (proactive batch)

graph.invoke / resume                              $ langmigrate upgrade head
        │                                                  │
        ▼                                                  ▼
MigrationInterceptor.get_tuple()                   adapter enumerates stale
  ├─ read checkpoint                               checkpoints (indexed query,
  ├─ tag ≠ head? → run upgrade cascade             keyset-paginated)
  ├─ write back healed state                               │
  │   (idempotent: same checkpoint id,                     ▼
  │    parent chain intact)                        engine migrates each one
  ▼                                                and writes it back
your node sees the new schema
```

The same pair exists for stores: `MigrationStore` (lazy, heals on `get()`) and
`langmigrate store upgrade` (batch). History enumeration (`list`/`search`) migrates
**in memory only** — no write storms, no rewriting of past checkpoints.

## Installation

```bash
pip install langmigrate                  # core (CLI + runtime, no DB drivers)
pip install "langmigrate[postgres]"      # + Postgres adapter
pip install "langmigrate[redis]"         # + Redis adapter
pip install "langmigrate[langchain]"     # + SchemaMigrationMiddleware (langchain 1.x agents)
```

Python 3.10–3.13. The core has no database dependencies — drivers are optional extras.

## Quickstart

Initialize once, then write a revision per schema change:

```bash
langmigrate init
langmigrate revision -m "add context field"
# or let LangMigrate diff your state schema and fill the body for you:
langmigrate revision -m "add context field" \
    --autogenerate --schema myapp.state:AgentState
```

A revision is a function pair — no subclassing required:

```python
from langmigrate import migration

@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state):
    return state.add_field("context", factory=dict)

@add_context.reverse
def _(state):
    return state.drop_field("context")
```

(The classic `class Migration(BaseMigration)` style still works and is what
`langmigrate revision` scaffolds. Declarative helpers: `add_field`, `drop_field`,
`rename_field`, `coerce_field`, `require_field`, plus `remap_node` for topology repair.)

Lazy online migration wraps your existing saver. `setup_langmigrate` is the
one-liner that builds the registry, engine and interceptor for you:

```python
from langmigrate import setup_langmigrate

saver = setup_langmigrate(base_saver, "migrations")   # write-back on by default
graph = builder.compile(checkpointer=saver)
```

<details><summary>...or wire it by hand for full control</summary>

```python
from langmigrate import MigrationInterceptor, MigrationEngine, MigrationRegistry

engine = MigrationEngine(MigrationRegistry.from_path("migrations"))
saver = MigrationInterceptor(base_saver, engine, write_back=True)
```

</details>

Cure the whole database proactively before (or instead of) lazy healing:

```bash
langmigrate upgrade head                  # batch-upgrade every stale checkpoint
langmigrate upgrade head --online-dry-run # validate the full cascade without writing
langmigrate current --db                  # revision distribution across the DB
```

## CLI at a glance

| Command | What it does |
| --- | --- |
| `langmigrate init [--with-store]` | Scaffold config + migrations directory (and store migrations) |
| `langmigrate revision -m "..."` | Create a new revision (`--autogenerate --schema mod:Class` diffs your state model) |
| `langmigrate merge -m "..."` | Join branched heads with a merge revision (multi-parent DAG) |
| `langmigrate history` | Show the revision DAG |
| `langmigrate current [--db]` | Show code head, or revision distribution in the database |
| `langmigrate check` | Validate the registry (broken pointers, unreachable heads) |
| `langmigrate upgrade <rev>` | Batch-upgrade stale checkpoints (`--online-dry-run`, `--continue-on-error`) |
| `langmigrate downgrade <rev>` | Batch-downgrade (`--dry-run`; irreversible migrations raise) |
| `langmigrate stamp <rev>` | Tag checkpoints without running migrations |
| `langmigrate store <verb>` | The same verbs (`revision`, `history`, `current`, `check`, `upgrade`, `downgrade`, `stamp`) for store items |

## Integration paths

| Your situation | Use | Where |
| --- | --- | --- |
| You own the checkpointer (Postgres/Redis/custom saver) | `MigrationInterceptor` via `setup_langmigrate` | [Quickstart](#quickstart) |
| Managed platform (LangGraph Server / Cloud / Studio) — no saver access | `SchemaMigrationMiddleware` or a `migrate_state_update` node | [docs/INTEGRATION.md](./docs/INTEGRATION.md) |
| Cross-thread memory (`BaseStore` items) | `MigrationStore` via `setup_langmigrate_store` | below |
| Pre-release bulk cure of the whole DB | `langmigrate upgrade` / `langmigrate store upgrade` | [CLI](#cli-at-a-glance) |

**Don't own the checkpointer (e.g. LangGraph Server)?** Migrate at the state level
with the middleware shim instead:

```python
from langmigrate.integrations.langchain import SchemaMigrationMiddleware

agent = create_agent(model, middleware=[SchemaMigrationMiddleware("migrations"), ...])
```

**Long-term memory (BaseStore) items** evolve too. Store migrations live in their
own directory and the wrapper is symmetric to the checkpointer one:

```python
from langmigrate import setup_langmigrate_store

store = setup_langmigrate_store(base_store, "store_migrations")
# pass `store` to your compiled LangGraph as the store
```

```bash
langmigrate init --with-store
langmigrate store revision -m "add kind field"
langmigrate store upgrade head           # proactive batch (Postgres)
```

## Compatibility matrix

| Change                                     | Safety | Strategy                                     |
| ------------------------------------------ | ------ | -------------------------------------------- |
| Add field with default                     | Safe   | lazy default injection                       |
| Remove unused field                        | Safe   | payload cleanup                              |
| Rename field                               | Unsafe | dynamic key remap                            |
| Change field type                          | Unsafe | registered coercion function                 |
| Add required field (no default)            | Unsafe | block with structured error or fallback hook |
| Interrupted thread on deleted/renamed node | Unsafe | [`NodeRemap`](./docs/INTEGRATION.md#topology-repair) helper applied within a migration |

## Architecture

Clean Architecture, strictly enforced: migration business logic is pure Python with **zero
database dependencies** — DB drivers only ever appear in adapters, as optional extras.

```text
   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │    cli/    │   │  runtime/  │   │  adapters/ │
   │ Typer app  │   │ Interceptor│   │ Postgres,  │
   │ (batch)    │   │ + Store    │   │ Redis      │
   └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
         │                │                │     (DB clients live only here,
         └────────────────┼────────────────┘      imported lazily)
                          ▼
   ┌─────────────────────────────────────────────┐
   │                   core/                     │
   │  types · operations · migration · registry  │
   │  engine · version · topology                │
   │           pure: no I/O, no DB drivers       │
   └─────────────────────────────────────────────┘
```

Key design decisions (full rationale in [CLAUDE.md](./CLAUDE.md)):

- **Alembic-style revision DAG** (`revision` + `down_revision`, tuple parents for merges)
  with deterministic path resolution.
- **Version tag in `checkpoint.metadata` (`langmigrate_rev`)** — queryable at the DB level
  (indexed by `setup()`), never polluting application state. Store items keep the tag inside
  `Item.value`, invisible to application code.
- **Idempotent lazy write-back**, on by default and disableable: re-persisting a migrated
  checkpoint never changes `checkpoint["id"]` nor breaks the `parent_config` chain.
- **Rollback safety**: unknown stored revisions (code rolled back after a lazy migration)
  are governed by the `on_unknown_revision` policy (`raise` / `warn` / `pass`).
- Every migration is **pure and idempotent**; every `upgrade` has a `downgrade` (or
  explicitly raises `IrreversibleMigrationError`).

## Runnable examples

The [`examples/`](./examples/) directory has end-to-end demos of every integration path —
all runnable with the in-memory saver, **no Docker required** (unless noted) — plus a
decision tree to pick the right one:

| Example | Pattern |
| --- | --- |
| [`quickstart`](./examples/quickstart/) | Online lazy in one line (`setup_langmigrate`), `mypy --strict`-clean |
| [`evolving_agent`](./examples/evolving_agent/) | Interceptor + write-back baseline: add / rename / coerce |
| [`middleware_agent`](./examples/middleware_agent/) | Managed platform: `SchemaMigrationMiddleware`, migrate node |
| [`multi_tool_agent`](./examples/multi_tool_agent/) | 3-revision cascade on a `StateGraph` |
| [`deep_research_agent`](./examples/deep_research_agent/) | `NodeRemap`, irreversible migrations, staged partial upgrade |
| [`batch_migration`](./examples/batch_migration/) | Offline batch: upgrade / downgrade / dry-run |
| [`studio`](./examples/studio/) | **LangGraph Studio**: break threads & store items live, then heal them |

If you want to *see* the failure before fixing it, start with the
[LangGraph Studio walkthrough](./examples/studio/README.md): a real `langgraph.json`
project where you break checkpointed threads and shared store items live in Studio
(`ValidationError` on resume after adding a required field, `KeyError` after a rename,
a store item stuck on the old value shape) and then heal each one with the migrate node,
`SchemaMigrationMiddleware`, or `MigrationStore`.

## Documentation

- **[Integration guide](./docs/INTEGRATION.md)** — saver-level vs state-level paths,
  topology repair, authoring migrations, a worked LangGraph Server + deepagents example.
- **[Docs site](https://scinfu.github.io/langmigrate/)** — rendered documentation + cookbook.
- **[CHANGELOG](./CHANGELOG.md)** — release notes.
- **[CLAUDE.md](./CLAUDE.md)** — architecture and contribution conventions.

## Status

**Stable (1.1).** Postgres and Redis adapters are implemented for both the proactive batch
and lazy online paths; 1.1 adds merge revisions (multi-parent DAG), LangGraph **store**
migrations (`MigrationStore` + `langmigrate store`), an async batch path, batch error
tolerance (`--continue-on-error`), a validating dry-run, and an `on_unknown_revision`
policy for rollback safety. The CLI, the runtime interceptors, and the state-level
middleware are covered by unit and integration tests on every supported Python version
(3.10–3.13). See [SECURITY.md](./SECURITY.md) for vulnerability reporting.

## Contributing

```bash
git clone https://github.com/scinfu/langmigrate && cd langmigrate
uv sync --extra dev --extra postgres --extra redis --extra langchain
uv run pytest                                # unit tests
docker compose up -d && uv run pytest -m integration   # integration tests
uv run ruff check . && uv run ruff format .  # lint + format
```

Conventions live in [CLAUDE.md](./CLAUDE.md) and [CONTRIBUTING.md](./CONTRIBUTING.md).
Issues and PRs welcome: <https://github.com/scinfu/langmigrate/issues>.

## License

[MIT](./LICENSE)
