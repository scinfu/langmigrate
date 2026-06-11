# LangMigrate × LangGraph Studio

A real LangGraph project (`langgraph.json`, no demo scripts) you run with
`langgraph dev` and drive entirely from **LangGraph Studio**: chat to create
threads, evolve the schema, watch old threads **break**, then enable LangMigrate
and watch them **heal**.

## Why these patterns (and not `MigrationInterceptor`)

In Studio / LangGraph Server the platform owns the checkpointer — graphs compiled
with a custom saver have it replaced, so the saver-wrap path
(`MigrationInterceptor` / `setup_langmigrate`) cannot run here. This project
demonstrates every LangMigrate path that **does** work on the managed platform:

| Graph | Path | LangMigrate API |
|---|---|---|
| `chat` | Dedicated `migrate` entry node | `migrate_state_update` |
| `agent` | Agent middleware stack | `SchemaMigrationMiddleware` |
| `memory` | Cross-thread store items | `setup_langmigrate_store` (`MigrationStore`) |

For the saver-wrap and batch-CLI paths (when you *do* own the checkpointer), see
the other examples: `quickstart`, `evolving_agent`, `multi_tool_agent`,
`batch_migration`.

## Setup

```bash
cd examples/studio
uv sync                      # installs langmigrate (editable, from ../..) + langgraph-cli
uv run langgraph dev         # starts the dev server and opens Studio
```

Studio opens at
`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`.
Pick a graph from the dropdown (`chat`, `agent`, `memory`).

No API key is required: `chat` and `memory` are deterministic echo bots, and
`agent` falls back to an offline echo model. To chat with a real LLM in the
`agent` graph:

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # optional
export STUDIO_AGENT_MODEL=anthropic:claude-opus-4-8   # optional override
```

Two facts make the demo work:

- `langgraph dev` **hot-reloads** when you edit a `graph.py` — no restart needed.
- Threads and the store are persisted under `.langgraph_api/`, so they
  **survive reloads** — exactly like a production DB surviving a deploy.

Every `graph.py` starts with the same two toggles:

```python
SCHEMA_VERSION = 1          # which version of the code is "deployed"
LANGMIGRATE_ENABLED = False # whether LangMigrate heals stale state
```

## How every test works

The cycle is always the same three phases. You never edit the thread state by
hand — the threads *are* the legacy data. What you change is the **deployed
code version**, by editing the two toggles in the graph's `graph.py`:

| Phase | Toggles | What you do | What you see |
|---|---|---|---|
| 1. v1 | `SCHEMA_VERSION = 1`, `LANGMIGRATE_ENABLED = False` | chat normally | threads created with the old schema |
| 2. break | `SCHEMA_VERSION = 2` | resume an old thread | `SchemaOutOfDateError` |
| 3. heal | `LANGMIGRATE_ENABLED = True` | resume the same thread | migration runs, reply works, state carries `langmigrate_rev` |

After each edit just **save the file**: the `langgraph dev` terminal logs the
reload — no restart, threads survive. Then go back to Studio and send the next
message.

---
## All Tests

```bash
cd examples/studio
uv sync                      # installs langmigrate (editable, from ../..) + langgraph-cli
uv run langgraph dev         # starts the dev server and opens Studio
```

## Test 1 — `chat` (migrate-node path)

File to edit: `studio_graphs/chat/graph.py`.

**Phase 1 — create legacy threads (v1)**

1. In Studio select the `chat` graph and send a message (e.g. `ciao`).
   → Reply: `[v1] You said: 'ciao'`.
2. Click **+ New Thread** and repeat, so you have 2–3 threads.
3. Open a thread's **state panel**: it contains only `messages` — no
   `language`, no `reply_count`, no `langmigrate_rev`. This is your "legacy DB".

**Phase 2 — deploy v2 and watch it break**

4. Edit `studio_graphs/chat/graph.py`:

   ```python
   SCHEMA_VERSION = 2
   ```

   Save and wait for the reload log in the `langgraph dev` terminal.
5. Back in Studio, open one of the old threads and send another message.
   → The run **fails** with:

   ```
   SchemaOutOfDateError: This thread was persisted with schema v1 and is
   missing the 'language' / 'reply_count' channels required by v2. ...
   ```

   (A brand-new thread fails too — nothing seeds the new fields. That's the
   point: v2 code, v1 data.)

**Phase 3 — enable LangMigrate and heal**

6. Edit the same file:

   ```python
   LANGMIGRATE_ENABLED = True
   ```

   Save, wait for the reload.
7. Resume the **same thread** that just failed and send a message.
   → Reply: `[v2 · english · reply #1] Hello! You said: ...` — the `migrate`
   node applied the cascade `a1c0_add_language` → `b2d1_add_reply_count`.
8. Check the state panel: it now shows `language: "english"`, `reply_count`,
   and `langmigrate_rev: "b2d1"` (the head revision).
9. Send one more message: the migration is a **no-op** (the tag is already at
   head) and `reply_count` keeps incrementing — lazy migration is idempotent.

**Bonus:** edit the thread state in Studio (or pass `language` in the input
form) and set `language: "italian"` → the greeting becomes `Ciao!`.

## Test 2 — `agent` (middleware path)

File to edit: `studio_graphs/agent/graph.py`.

**Phase 1 — v1**

1. Select the `agent` graph and chat in a couple of threads. With
   `ANTHROPIC_API_KEY` set it's a real Claude agent (try `how much is 21+21?`
   to see the `add_numbers` tool); without a key it's an offline echo model —
   the migration demo is identical.

**Phase 2 — break**

2. Set `SCHEMA_VERSION = 2`, save, wait for the reload.
3. Send a message in any thread (old or new).
   → `SchemaOutOfDateError: Schema v2 requires the 'user_profile' channel ...`
   — the new `ProfileMiddleware` reads a channel no thread has.

**Phase 3 — heal**

4. Set `LANGMIGRATE_ENABLED = True`, save, wait for the reload.
5. Resume the failed thread.
   → The agent answers again: `SchemaMigrationMiddleware` runs first in the
   middleware stack (hooks `before_agent` + `before_model`) and applies
   `a1c0_add_user_profile` before `ProfileMiddleware` reads the state.
6. Check the thread state: it now carries
   `user_profile: {"name": "guest", "tone": "friendly"}` and
   `langmigrate_rev: "a1c0"`. New threads work too — untagged state is treated
   as the base revision and upgraded the same way.

## Test 3 — `memory` (store path)

File to edit: `studio_graphs/memory/graph.py`. Here the *thread* state never
breaks — the **store item** does. The store is shared across threads, so you
can break it in one thread and heal it from another.

**Phase 1 — v1**

1. Select the `memory` graph and send: `save Mario`.
   → `[v1] Saved profile: {'name': 'Mario'}` — a flat v1 item is persisted.
2. Send anything else (e.g. `who am I?`).
   → `[v1] Stored name: 'Mario'`.

**Phase 2 — break**

3. Set `SCHEMA_VERSION = 2`, save, wait for the reload.
4. Ask again (any thread, even a new one):
   → `SchemaOutOfDateError: The stored item still has the v1 shape
   {'name': ...} but the v2 code expects {'profile': {...}, 'language': ...}`.

**Phase 3 — heal**

5. Set `LANGMIGRATE_ENABLED = True`, save, wait for the reload.
6. Ask again.
   → `[v2] Stored name: 'Mario' (language: english)` — `MigrationStore` healed
   the item on `get()` (revision `s1a0_nest_profile`) and **wrote it back**:
   the persisted value is now
   `{"profile": {"name": "Mario"}, "language": "english"}` plus a revision tag
   kept *inside the value* and stripped from every read — neither the graph
   nor the migrations ever see it.
7. Ask once more: same answer, no migration this time (the item is at head).

---

## After the demo

Reset the toggles in all three `graph.py` files back to the defaults
(`SCHEMA_VERSION = 1`, `LANGMIGRATE_ENABLED = False`) if you want to run the
walkthrough again from phase 1.

## Resetting the demo

Stop the server and delete the dev persistence to start from scratch:

```bash
rm -rf .langgraph_api
```

## Layout

```
examples/studio/
├── langgraph.json                 # 3 graphs served by `langgraph dev`
├── pyproject.toml                 # langmigrate installed editable from ../..
└── studio_graphs/
    ├── common.py                  # SchemaOutOfDateError + helpers
    ├── chat/
    │   ├── graph.py               # migrate node (migrate_state_update)
    │   └── migrations/            # a1c0_add_language, b2d1_add_reply_count
    ├── agent/
    │   ├── graph.py               # create_agent + SchemaMigrationMiddleware
    │   ├── fake_model.py          # keyless fallback model
    │   └── migrations/            # a1c0_add_user_profile
    └── memory/
        ├── graph.py               # MigrationStore over the platform store
        └── store_migrations/      # s1a0_nest_profile
```
