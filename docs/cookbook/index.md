---
layout: default
title: LangMigrate Cookbook
nav_order: 2
has_children: true
permalink: /cookbook/
---

# LangMigrate Cookbook

Practical recipes for the most common LangGraph schema migration scenarios.
Every recipe is self-contained, copy-paste ready, and runnable with `InMemorySaver`
so you do not need a database to follow along.

---

## Quick-reference matrix

| Scenario | Path | Recipe |
|---|---|---|
| Add a field with a default | Either | [Recipe 1 — add a field](#recipe-1) |
| Remove an unused field | Either | [Recipe 2 — drop a field](#recipe-2) |
| Rename a field | Saver path | [Recipe 3 — rename a field](#recipe-3) |
| Change a field's type | Either | [Recipe 4 — coerce a field](#recipe-4) |
| Add a required field (no default) | Either | [Recipe 5 — require a field](#recipe-5) |
| Repair a renamed graph node | Saver path | [Recipe 6 — topology repair](#recipe-6) |
| Cure the DB before a release | Saver path | [Recipe 7 — batch upgrade](#recipe-7) |
| Emergency rollback | Saver path | [Recipe 8 — batch downgrade](#recipe-8) |
| Managed platform (LangGraph Server) | State path | [Recipe 9 — state-level middleware](#recipe-9) |
| Hand-built StateGraph, no saver access | State path | [Recipe 10 — migrate node](#recipe-10) |
| Irreversible migration (data drop) | Either | [Recipe 11 — irreversible migration](#recipe-11) |
| Staged / canary rollout | Saver path | [Recipe 12 — partial upgrade](#recipe-12) |
| Autogenerate from schema diff | Either | [Recipe 13 — autogenerate](#recipe-13) |

---

## Two paths at a glance

```
Do you own the checkpointer?
        │
        ├── YES → Path A: MigrationInterceptor (saver-level)
        │           • Lazy on load  (online)
        │           • Batch CLI     (offline: langmigrate upgrade head)
        │           • Full channel removal supported
        │
        └── NO  → Path B: state-level
                    • SchemaMigrationMiddleware  (managed platform)
                    • migrate_state_update node  (hand-built StateGraph)
                    ⚠ Channel rename/drop NOT supported (LangGraph merges state)
```

---

## Setup (all recipes)

```bash
uv add langmigrate
# postgres extra:  uv add "langmigrate[postgres]"
# redis extra:     uv add "langmigrate[redis]"
# middleware extra: uv add "langmigrate[langchain]"

langmigrate init             # creates langmigrate.toml + a scaffolded migrations/
langmigrate init --example   # ...and a first (empty) revision skeleton
```

`init` scaffolds `migrations/` as a Python package (`__init__.py` + `README.md`).

### Two authoring styles

Every recipe below uses the **class style** (`class Migration(BaseMigration)`), which is
what `langmigrate revision` scaffolds. The same revision can be written as a
**function pair** with the `@migration` decorator — less boilerplate, and mutations go
through the fluent `StateEnvelope` helpers (`state.add_field(...)`):

```python
from langmigrate import migration

@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state):
    return state.add_field("context", factory=dict)

@add_context.reverse                       # omit to declare the migration irreversible
def _(state):
    return state.drop_field("context")
```

Both styles are discovered by `MigrationRegistry.from_path("migrations")` and behave
identically. Pick one per file.

### Wiring the saver (Path A)

The one-liner `setup_langmigrate` builds the registry, engine and interceptor at once:

```python
from langmigrate import setup_langmigrate
from langgraph.checkpoint.memory import InMemorySaver

saver = setup_langmigrate(InMemorySaver(), "migrations")   # write-back on by default
graph = builder.compile(checkpointer=saver)
```

It accepts a path, a `MigrationRegistry`, or a `MigrationEngine`, and forwards
`write_back` / `target`. The explicit three-line form is shown in Recipe 1 for when you
need to hold the engine yourself.

---

<a id="recipe-1"></a>
## Recipe 1 — Add a field with a default

**Scenario:** you added `context: dict` to `AgentState` after some threads were already
persisted. Old threads lack the key; new code expects it.

**Safety:** Safe — the default is injected lazily; no data can be lost.

### Migration file (class style)

```python
# migrations/a1c0_add_context.py
from langmigrate import BaseMigration, StateEnvelope

class AddContext(BaseMigration):
    revision = "a1c0"
    down_revision = None   # first revision (base)
    slug = "add_context"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.add_field(state, "context", factory=dict)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.drop_field(state, "context")
```

### Migration file (function-pair style)

The exact same revision, with less ceremony:

```python
# migrations/a1c0_add_context.py
from langmigrate import StateEnvelope, migration

@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state: StateEnvelope) -> StateEnvelope:
    return state.add_field("context", factory=dict)

@add_context.reverse
def _(state: StateEnvelope) -> StateEnvelope:
    return state.drop_field("context")
```

### Wire up the interceptor

```python
from langmigrate import setup_langmigrate
from langgraph.checkpoint.memory import InMemorySaver

saver = setup_langmigrate(InMemorySaver(), "migrations", write_back=True)
graph = builder.compile(checkpointer=saver)
```

<details><summary>...or build the engine by hand</summary>

```python
from langmigrate import MigrationEngine, MigrationRegistry, MigrationInterceptor

engine = MigrationEngine(MigrationRegistry.from_path("migrations"))
saver  = MigrationInterceptor(InMemorySaver(), engine, write_back=True)
```

</details>

**What happens:** on the first `get_tuple` for a stale thread, the interceptor runs the
cascade, writes back the upgraded checkpoint (same `id`), and returns it already migrated.

---

<a id="recipe-2"></a>
## Recipe 2 — Drop an unused field

**Scenario:** `debug_trace` was removed from `AgentState`. Old threads carry it; you want
clean payloads.

**Safety:** Safe — no data is referenced by new code after this point.

```python
# migrations/b2d1_drop_debug_trace.py
from langmigrate import BaseMigration, StateEnvelope

class DropDebugTrace(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "drop_debug_trace"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.drop_field(state, "debug_trace")

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Re-add a placeholder so the schema can travel back.
        return self.add_field(state, "debug_trace", default=None)
```

> **Note:** `drop_field` is a no-op when the field is already absent — safe to re-run.

---

<a id="recipe-3"></a>
## Recipe 3 — Rename a field

**Scenario:** `msgs` was renamed to `messages` in `AgentState`. Old threads store `msgs`;
new code reads `messages`.

**Safety:** Unsafe — requires explicit handling. `rename_field` raises `UnsafeMigrationError`
if both keys coexist with different values.

```python
# migrations/b2d1_rename_msgs.py
from langmigrate import BaseMigration, StateEnvelope

class RenameMsgs(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "rename_msgs"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.rename_field(state, "msgs", "messages")

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.rename_field(state, "messages", "msgs")
```

> **Path A only.** `MigrationInterceptor` rebuilds `channel_values` wholesale, so the old
> key is physically removed. At the state level (Path B) LangGraph *merges* updates —
> `msgs` would linger. Use the saver path for renames.

---

<a id="recipe-4"></a>
## Recipe 4 — Coerce a field's type

**Scenario:** `count` was stored as a string (`"3"`) in early versions; the code now expects
an `int`.

**Safety:** Unsafe — the coercion may fail if the value is not castable.

```python
# migrations/b2d1_coerce_count.py
from langmigrate import BaseMigration, StateEnvelope

class CoerceCount(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "coerce_count"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.coerce_field(
            state, "count", int,
            skip_if=lambda v: isinstance(v, int),  # idempotent guard
        )

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.coerce_field(state, "count", str)
```

The `skip_if` guard makes the migration idempotent: re-running it on a thread that was
already coerced is a no-op.

---

<a id="recipe-5"></a>
## Recipe 5 — Add a required field (no default)

**Scenario:** `user_id` is now mandatory. Old threads have no value for it. You want to
either inject a sentinel or hard-block resumption until a human provides the value.

**Option A — inject a sentinel:**

```python
def upgrade(self, state: StateEnvelope) -> StateEnvelope:
    return self.require_field(state, "user_id", fallback="UNKNOWN")
```

**Option B — hard block (raises `MissingRequiredFieldError`):**

```python
def upgrade(self, state: StateEnvelope) -> StateEnvelope:
    return self.require_field(state, "user_id")
    # MissingRequiredFieldError is raised; catch it in the caller to surface a
    # meaningful error to the user instead of a deserialization failure.
```

**Option C — derive from existing state:**

```python
import uuid

def upgrade(self, state: StateEnvelope) -> StateEnvelope:
    return self.require_field(
        state, "user_id",
        factory=lambda: f"migrated-{uuid.uuid4()}"
    )
```

---

<a id="recipe-6"></a>
## Recipe 6 — Topology repair (renamed graph node)

**Scenario:** graph node `research_step` was renamed to `web_researcher`. Threads
interrupted mid-run on the old node deadlock on resume.

**Safety:** Safe — purely a metadata repair; no application data is changed.

```python
# migrations/b2d1_remap_research_node.py
from langmigrate import BaseMigration, StateEnvelope

class RemapResearchNode(BaseMigration):
    revision = "b2d1"
    down_revision = "a1c0"
    slug = "remap_research_node"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.remap_node(
            state,
            renames={"research_step": "web_researcher"},
            removed=["legacy_tool"],
            fallback="__start__",
            known_nodes=["planner", "web_researcher", "synthesizer", "__end__"],
        )

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.remap_node(
            state,
            renames={"web_researcher": "research_step"},
            known_nodes=["planner", "research_step", "synthesizer", "__end__"],
        )
```

`remap_node` only acts when `state.node` is set and it matches one of the listed old
names — threads that were not paused on the affected node are returned unchanged.

---

<a id="recipe-7"></a>
## Recipe 7 — Batch upgrade (cure the DB before a release)

Use this before deploying a breaking change so *every* thread arrives at the new code
already migrated, rather than waiting for lazy on-load migration.

### With Postgres

```bash
export LANGMIGRATE_URL="postgresql://user:pass@localhost:5432/mydb"

langmigrate upgrade head --online-dry-run   # preview: count stale checkpoints
langmigrate upgrade head                     # apply
langmigrate current --db             # verify: revision distribution
```

### Programmatic (custom adapter)

```python
from langmigrate import MigrationEngine, MigrationRegistry, run_batch_upgrade
from langmigrate.adapters.postgres import PostgresAdapter

engine  = MigrationEngine(MigrationRegistry.from_path("migrations"))
adapter = PostgresAdapter.from_conn_string("postgresql://...")
adapter.setup()

result = run_batch_upgrade(adapter, engine, dry_run=True)
print(f"Stale: {result.total}")

result = run_batch_upgrade(adapter, engine)
print(f"Migrated: {result.migrated}/{result.total}")
```

### BatchResult fields

| Field | Type | Meaning |
|---|---|---|
| `target` | `str` | Revision upgraded to (`"base"` for a full downgrade) |
| `total` | `int` | Checkpoints considered (stale count for upgrade; scanned count for downgrade) |
| `migrated` | `int` | Checkpoints changed — on a dry run, the count that *would* be migrated (nothing is written) |
| `dry_run` | `bool` | Whether this was a preview (no writes) |

---

<a id="recipe-8"></a>
## Recipe 8 — Batch downgrade (emergency rollback)

```bash
langmigrate downgrade b2d1    # roll back one revision
langmigrate downgrade base    # remove all migrations
```

Or programmatically:

```python
from langmigrate import run_batch_downgrade

result = run_batch_downgrade(adapter, engine, target="b2d1")
print(f"Rolled back: {result.migrated}/{result.total}")

# Full rollback to base (removes revision tag from all checkpoints):
result = run_batch_downgrade(adapter, engine, target=None)
```

> **Warning:** A migration that calls `self.raise_irreversible()` in `downgrade` will abort
> the batch when it is crossed. Design your migration chain so that irreversible revisions
> sit at the top (newest) of the DAG.

---

<a id="recipe-9"></a>
## Recipe 9 — State-level middleware (LangGraph Server / managed platform)

Use this when you **cannot** wrap the checkpointer (e.g. `langgraph dev`, LangGraph Cloud).

```bash
uv add "langmigrate[langchain]"
langmigrate init
langmigrate revision -m "add user_id" \
    --autogenerate --schema src.graphs.state:AgentState
```

```python
from langmigrate.integrations.langchain import SchemaMigrationMiddleware

migration = SchemaMigrationMiddleware("migrations")
agent = create_agent(model, tools=[...], middleware=[migration, ...])
```

The middleware implements both `before_agent` (once per fresh run) and `before_model`
(each model call, so mid-loop resumes are covered). Both hooks are idempotent.

**Limitations:**

- Channel rename / drop cannot be applied — LangGraph merges state updates, so the old
  key lingers. Use `MigrationInterceptor` (Recipe 3) for hard channel removal.
- A thread that resumes directly into a **tool node** sees pre-migration state until the
  next model call.
- Strict Pydantic schemas may fail to deserialize *before* the middleware runs — those
  need Path A.

---

<a id="recipe-10"></a>
## Recipe 10 — Migrate node (hand-built StateGraph)

No middleware, no saver access — just insert a `migrate` node at the graph entry point.

```python
from langmigrate import MigrationEngine, MigrationRegistry, migrate_state_update

engine = MigrationEngine(MigrationRegistry.from_path("migrations"))

def migrate_node(state: dict) -> dict | None:
    """Entry node: apply pending migrations idempotently."""
    return migrate_state_update(engine, state)

from langgraph.graph import END, StateGraph

graph = StateGraph(AgentState)
graph.add_node("migrate", migrate_node)
graph.add_node("agent", agent_node)
graph.set_entry_point("migrate")
graph.add_edge("migrate", "agent")
graph.add_edge("agent", END)
app = graph.compile()
```

`migrate_state_update` returns `None` when the state is already at head (no-op),
so the node is cheap on every run after the first.

Same limitation as Recipe 9: channel rename/drop is not supported via state updates.

---

<a id="recipe-11"></a>
## Recipe 11 — Irreversible migration (permanent data drop)

Use when data is intentionally discarded and rolling back would require information
that no longer exists.

```python
# migrations/c3e2_drop_pii.py
from langmigrate import BaseMigration, StateEnvelope

class DropPii(BaseMigration):
    revision = "c3e2"
    down_revision = "b2d1"
    slug = "drop_pii"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.drop_field(state, "raw_email")
        return self.drop_field(state, "ip_address")

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        # Data is gone — make the irreversibility explicit.
        self.raise_irreversible()
```

Attempting a batch downgrade that crosses this revision raises
`IrreversibleMigrationError`. The CLI surfaces it as a hard error rather than silently
corrupting the data.

```python
from langmigrate import IrreversibleMigrationError

try:
    run_batch_downgrade(adapter, engine, target="a1c0")
except IrreversibleMigrationError as e:
    print(f"Cannot downgrade: {e}")
```

---

<a id="recipe-12"></a>
## Recipe 12 — Staged / canary upgrade

Stop the cascade at an intermediate revision to run two versions of your graph in
parallel (canary deployment, A/B experiment).

```python
# Canary fleet: upgrade only to b2d1
canary_interceptor = MigrationInterceptor(
    base_saver, engine, write_back=True, target="b2d1"
)
canary_graph = builder.compile(checkpointer=canary_interceptor)

# Stable fleet: full upgrade to head
stable_interceptor = MigrationInterceptor(
    base_saver, engine, write_back=True   # target defaults to HEAD
)
stable_graph = builder.compile(checkpointer=stable_interceptor)
```

Both interceptors share the same underlying saver; each only migrates threads up to
its target. Threads written back by the canary remain upgradeable to head by the stable
fleet later.

---

<a id="recipe-13"></a>
## Recipe 13 — Autogenerate from schema diff

Point `--autogenerate` at your state class to scaffold a revision automatically.

```bash
langmigrate revision -m "add session_id" \
    --autogenerate --schema myapp.state:AgentState
```

LangMigrate diffs your class against the previous revision's snapshot and emits
`add_field` / `drop_field` / `coerce_field` calls in the body. **Always review the
output** before committing — autogenerate cannot infer:

- Whether a field was *renamed* (it sees an add + drop; you must change one to
  `rename_field`).
- The correct coercion function for type changes.
- Whether a removed field should use `raise_irreversible()`.

---

## Combining recipes

Migrations are applied as a linear cascade. You can freely combine primitives within a
single revision:

```python
class V3Migration(BaseMigration):
    revision = "d4f3"
    down_revision = "c3e2"
    slug = "v3_schema"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.add_field(state, "session_id", factory=lambda: str(uuid.uuid4()))
        state = self.rename_field(state, "user_input", "query")
        state = self.coerce_field(state, "iteration", int,
                                  skip_if=lambda v: isinstance(v, int))
        state = self.remap_node(state, renames={"tool": "run_tool"},
                                fallback="__start__")
        return state

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        state = self.rename_field(state, "query", "user_input")
        state = self.coerce_field(state, "iteration", str)
        return self.drop_field(state, "session_id")
```

---

## Error reference

| Exception | Cause | Resolution |
|---|---|---|
| `UnsafeMigrationError` | `rename_field` found both keys with different values | Investigate data; add a pre-check or fallback |
| `MissingRequiredFieldError` | `require_field` with no fallback, field absent | Add `fallback=` or `factory=` to the call |
| `IrreversibleMigrationError` | Downgrade crossed a `raise_irreversible()` migration | Design the DAG so irreversible revisions are at the top |
| `RevisionNotFoundError` | Target revision id not in the registry | Check spelling; run `langmigrate history` |
| `MultipleHeadsError` | Two revisions both claim to be head | Create a merge revision |
| `TopologyMismatchError` | `remap_node` found an unknown node after remap | Add the node to `known_nodes` or extend `renames` |
| `ChannelRemovalUnsupportedError` | State-level path tried to rename/drop a channel | Switch to `MigrationInterceptor` (Path A) |

---

## CLI cheat sheet

```bash
langmigrate init                              # bootstrap config + scaffolded migrations/
langmigrate init --example                    # ...plus a first revision skeleton
langmigrate revision -m "describe change"    # new revision chained to head
langmigrate revision -m "..." \
    --autogenerate --schema app.state:State  # scaffold from schema diff
langmigrate history                          # list all revisions
langmigrate check                            # validate DAG integrity
langmigrate current                          # head revision id
langmigrate current --db                     # revision distribution in DB
langmigrate upgrade head                     # migrate all stale checkpoints
langmigrate upgrade head --online-dry-run    # preview (no writes)
langmigrate upgrade <rev>                    # upgrade to a specific revision
langmigrate downgrade <rev>                  # roll back to a revision
langmigrate downgrade base                   # remove all migration tags
langmigrate stamp <rev>                      # mark checkpoints without migrating
```

---

## Further reading

- [Integration guide](../INTEGRATION.md) — deep dive on Path A vs Path B, topology
  repair, and the LangGraph Server pattern.
- [Examples](../../examples/) — runnable demos covering each pattern end-to-end,
  including a `mypy --strict` quickstart using `setup_langmigrate` + `@migration`.
- [API reference](https://scinfu.github.io/langmigrate/) — auto-generated from
  docstrings.
