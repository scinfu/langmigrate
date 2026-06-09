# LangMigrate

> Declarative schema migrations for LangGraph state persistence — Alembic for your checkpointers.

LangGraph persists application state through *checkpointers* (Postgres, Redis, ...) so graphs
can pause, resume, and survive failures. But as your app evolves, the state schema
(`TypedDict` / Pydantic) changes — fields get added, removed, renamed, retyped. Old or
interrupted threads resumed on newer code then fail to deserialize or silently corrupt data.

**LangMigrate** fixes this with declarative, versioned migrations applied either:

- **Proactively (batch)** — an offline CLI that walks every checkpoint in the database and
upgrades it, or
- **Lazily (online)** — a runtime interceptor that upgrades a thread on the fly the moment it
is loaded, via a cascade of transformation functions.

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
- **Resuming an interrupted thread after a graph refactor silently loses work** — the
  scariest variant, because there is *no* exception. A thread paused mid-node (e.g. on a
  human-in-the-loop `interrupt()`) is resumed on code where that node was renamed or removed;
  LangGraph can't reattach the pending task, so the in-flight decision is dropped and the
  resumed run returns stale state. No stack trace, no log line — just `langgraph interrupt
  resume not working` / silent state corruption after a deploy (topology drift).
- **"It worked before the deploy"** — Postgres/Redis checkpointer threads created on an
  older schema crash, silently lose data, or corrupt state on the new code.

These are all the same root cause: a LangGraph **checkpointer persisted state under an old
schema**, and your new code can't read it. LangMigrate versions and migrates that state the
way Alembic does for SQL — see below.

## Compatibility matrix


| Change                                     | Safety | Strategy                                     |
| ------------------------------------------ | ------ | -------------------------------------------- |
| Add field with default                     | Safe   | lazy default injection                       |
| Remove unused field                        | Safe   | payload cleanup                              |
| Rename field                               | Unsafe | dynamic key remap                            |
| Change field type                          | Unsafe | registered coercion function                 |
| Add required field (no default)            | Unsafe | block with structured error or fallback hook |
| Interrupted thread on deleted/renamed node | Unsafe | [`NodeRemap`](./docs/INTEGRATION.md#topology-repair) helper applied within a migration |


## Status

**Stable (1.0).** Postgres and Redis adapters are implemented for both the
proactive batch and lazy online paths. The CLI, the runtime interceptor, and
the state-level middleware are covered by unit and integration tests on
every supported Python version (3.10–3.13). See the
[CHANGELOG](./CHANGELOG.md) for release notes and [SECURITY.md](./SECURITY.md)
for vulnerability reporting.

## Quickstart

```bash
uv sync --extra dev --extra postgres --extra redis --extra langchain
docker compose up -d

uv run langmigrate init
uv run langmigrate revision -m "add context field"
# or let LangMigrate diff your state schema and fill the body for you:
uv run langmigrate revision -m "add context field" \
    --autogenerate --schema myapp.state:AgentState

uv run langmigrate upgrade head          # proactive batch
uv run langmigrate current --db          # revision distribution in the DB
```

Writing a revision is a function pair — no subclassing required:

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
`langmigrate revision` scaffolds.)

Lazy online migration wraps your existing saver. `setup_langmigrate` is the
one-liner that builds the registry, engine and interceptor for you:

```python
from langmigrate import setup_langmigrate

saver = setup_langmigrate(base_saver, "migrations")   # write-back on by default
# pass `saver` to your compiled LangGraph as the checkpointer
```

<details><summary>...or wire it by hand for full control</summary>

```python
from langmigrate import MigrationInterceptor, MigrationEngine, MigrationRegistry

engine = MigrationEngine(MigrationRegistry.from_path("migrations"))
saver = MigrationInterceptor(base_saver, engine, write_back=True)
```

</details>

**Don't own the checkpointer (e.g. LangGraph Server)?** Migrate at the state level
with the middleware shim instead — see [docs/INTEGRATION.md](./docs/INTEGRATION.md):

```python
from langmigrate.integrations.langchain import SchemaMigrationMiddleware

agent = create_agent(model, middleware=[SchemaMigrationMiddleware("migrations"), ...])
```

## Design

See [CLAUDE.md](./CLAUDE.md) for architecture and contribution conventions. Key decisions:

- **Alembic-style revision DAG** (`revision` + `down_revision`).
- **Version tag stored in `checkpoint.metadata` (`langmigrate_rev`)** — queryable at the DB
level, never polluting your application state.
- **Idempotent lazy write-back**, on by default and disableable.
- **Clean Architecture**: migration logic is fully decoupled from DB client libraries.

## License

MIT