---
layout: default
title: Integration Guide
nav_order: 1
permalink: /integration/
---

# Integrating LangMigrate

There are **two** ways to apply migrations. Which one fits depends on a single
question: **do you own the checkpointer instance?**

| You control the saver? | Use | How |
|---|---|---|
| **Yes** (self-hosted `PostgresSaver` / `RedisSaver` / `InMemorySaver`) | `MigrationInterceptor` | wrap the saver; lazy on load + batch CLI |
| **No** (LangGraph Server / managed platform) | `SchemaMigrationMiddleware` | migrate the state in `before_agent` / `before_model` |

You do **not** need a database to use LangMigrate. A database only enters the
picture if you already persist LangGraph state in one and want the proactive batch
path. The optional `[postgres]` / `[redis]` extras are only for that.

## Path A — saver-level (you own the checkpointer)

```python
from langmigrate import setup_langmigrate

saver = setup_langmigrate(PostgresSaver(...), "migrations")   # write-back on by default
graph = builder.compile(checkpointer=saver)
```

`setup_langmigrate(saver, migrations)` builds the registry, engine and interceptor
in one call. `migrations` accepts a path (`str` / `Path`), a `MigrationRegistry`, or a
ready-made `MigrationEngine`; `write_back` and `target` are forwarded to the
interceptor. The explicit form below is equivalent if you need to hold the engine:

```python
from langmigrate import MigrationEngine, MigrationRegistry, MigrationInterceptor

engine = MigrationEngine(MigrationRegistry.from_path("migrations"))
saver = MigrationInterceptor(PostgresSaver(...), engine, write_back=True)
graph = builder.compile(checkpointer=saver)
```

- **Lazy online:** old threads upgrade on load; the DB self-heals (idempotent
  write-back, checkpoint id preserved).
- **Proactive batch:** `langmigrate upgrade head` walks every stored checkpoint.

This is the most complete path (the version tag lives in `checkpoint.metadata`,
queryable at the DB level).

## Path B — state-level middleware (managed platform, e.g. LangGraph Server)

When the platform owns the checkpointer (you only `compile()`/declare the graph and
never pass a saver), wrap the migration as middleware instead:

```python
from langmigrate.integrations.langchain import SchemaMigrationMiddleware

migration = SchemaMigrationMiddleware("migrations")  # path or a MigrationEngine
agent = create_agent(model, middleware=[migration, ...])
```

The middleware migrates state at the **earliest hook it reaches**: it implements
both `before_agent` (once, at the start of a fresh pass) and `before_model` (each
model call, so mid-loop resumes are covered). Both are idempotent — after the first
migration they return `None`.

Notes and trade-offs:

- **Timing is best-effort, not a hard guarantee.** Middleware hooks are graph nodes,
  and a resume re-enters at the *interrupted* node. A thread that resumes directly
  into a **tool node** sees pre-migration state until the next hook runs. `before_agent`
  is **not** re-run on a mid-loop resume; `before_model` is, but only at the next model
  call. For a strict "before every node" guarantee, own the checkpointer and use
  **Path A** (`MigrationInterceptor`).
- **Only applies to middleware-based agents** (`create_agent` / deepagents). A
  hand-built `StateGraph` has no middleware hooks — call the pure helper in your own
  entry node instead (below), or use Path A.
- The revision tag is carried as a **reserved state channel** (`langmigrate_rev`,
  configurable). The middleware declares it via its `state_schema`, so you don't
  have to change your own state — but if your framework version doesn't merge
  middleware schemas, add `langmigrate_rev: NotRequired[str]` to your state.
- Works cleanly with **`TypedDict`** state (LangGraph is permissive on load, so old
  threads deserialize and the middleware then normalizes them). Strict **Pydantic**
  schemas may fail to deserialize *before* the middleware runs — those need Path A.
- **Channel removal can't be expressed at the state level — this includes renames.**
  LangGraph *merges* updates, so a `rename_field("msgs", "messages")` adds `messages`
  but leaves the old `msgs` key lingering; a `drop_field` likewise can't delete the
  channel. The helper surfaces this via `on_removed` (`"warn"` default / `"error"` /
  `"ignore"`). Migrations that must truly purge old channels need **Path A**
  (`MigrationInterceptor` rebuilds `channel_values` wholesale and removes them). Prefer
  add/coerce in state-level migrations; reserve rename/drop for the saver path.

For a hand-built `StateGraph`, run the pure helper in your own entry node (and make
it the unconditional entry point so it precedes the other nodes):

```python
from langmigrate.integrations.state import migrate_state_update

def migrate_node(state):
    return migrate_state_update(engine, state, target="head") or {}
```

## Topology repair

When a graph node is **renamed or removed** mid-deployment, interrupted
threads that paused on the old node resume pointing at a graph position
that no longer exists. `NodeRemap` repairs them from inside a migration:

```python
from langmigrate import BaseMigration, NodeRemap

class RenameToolNode(BaseMigration):
    revision = "c3e2_rename_tool"
    down_revision = "b2d1_rename_msgs"

    def upgrade(self, state):
        # Remap interrupted threads paused on the old node name
        # to the new one (or a fallback for removed nodes).
        state = self.remap_node(
            state,
            renames={"tool": "run_tool"},
            removed=["legacy_tool"],
            fallback="__start__",
            known_nodes=["agent", "run_tool", "tools", "__end__"],
        )
        return state

    def downgrade(self, state):
        state = self.remap_node(
            state,
            renames={"run_tool": "tool"},
            known_nodes=["agent", "tool", "tools", "__end__"],
        )
        return state
```

`remap_node` only acts when `state.node` is set — pass it from your own
checkpoint inspection (e.g. via `tup.metadata["writes"]` or the node
name stored in `checkpoint["channel_values"]` by LangGraph). Migrations
that don't need topology repair can simply not call it; the helper is
opt-in.

## Authoring migrations

Two styles, both discovered by `MigrationRegistry.from_path`:

**Class style** (what `langmigrate revision` scaffolds):

```python
from langmigrate import BaseMigration, StateEnvelope

class AddContext(BaseMigration):
    revision = "a1c0"
    down_revision = None
    slug = "add_context"

    def upgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.add_field(state, "context", factory=dict)

    def downgrade(self, state: StateEnvelope) -> StateEnvelope:
        return self.drop_field(state, "context")
```

**Function-pair style** (`@migration`, less boilerplate) — mutations use the fluent
`StateEnvelope` helpers; attach the reverse with `.reverse` (omit it to declare the
migration irreversible):

```python
from langmigrate import migration

@migration("a1c0", down_revision=None, slug="add_context")
def add_context(state):
    return state.add_field("context", factory=dict)

@add_context.reverse
def _(state):
    return state.drop_field("context")
```

`langmigrate check` reports any revision that is missing a downgrade.

### Scaffolding from your state schema

Point `--autogenerate` at your state class to scaffold a revision:

```bash
langmigrate revision -m "add user_id" --autogenerate --schema myapp.state:AgentState
```

It diffs your schema against the previous revision's snapshot and fills the body
with `add_field` / `drop_field` / `coerce_field` calls (review defaults, coercions
and possible renames — those need human judgement).

### Worked example: a LangGraph Server + deepagents project

For a project running on `langgraph dev` with a `TypedDict` `AgentState` and a
middleware stack:

```bash
uv add "langmigrate[langchain]"             # pulls langchain >= 1 for the middleware base class
langmigrate init
langmigrate revision -m "baseline" \
    --autogenerate --schema src.graphs.agent_state:AgentState
```

Then add `SchemaMigrationMiddleware("migrations")` to the agent's middleware list.
From then on, each `AgentState` change gets a reviewed revision, and old threads in
the dev store are upgraded transparently on the next step.
