---
layout: default
title: Home
nav_order: 0
permalink: /
---

# LangMigrate

> Declarative schema migrations for LangGraph state persistence — Alembic for your checkpointers.

LangGraph persists application state through checkpointers (Postgres, Redis, ...) so
graphs can pause, resume, and survive failures. As your app evolves, the state schema
changes — fields get added, removed, renamed, retyped. Old threads then fail to
deserialize or silently corrupt data.

**LangMigrate** fixes this with declarative, versioned migrations applied either:

- **Proactively (batch)** — an offline CLI that walks every checkpoint and upgrades it, or
- **Lazily (online)** — a runtime interceptor that upgrades a thread on the fly the moment
  it is loaded.

---

## Documentation

| | |
|---|---|
| [Integration Guide](integration/) | Path A (saver) vs Path B (state), topology repair, LangGraph Server pattern |
| [Cookbook](cookbook/) | 13 copy-paste recipes for every common scenario |

---

## Quickstart

```bash
uv add langmigrate
langmigrate init
langmigrate revision -m "add context field"
langmigrate upgrade head
```

Lazy online migration — one line wires the registry, engine and interceptor:

```python
from langmigrate import setup_langmigrate

saver = setup_langmigrate(base_saver, "migrations")   # write-back on by default
graph = builder.compile(checkpointer=saver)
```

---

## Compatibility matrix

| Change | Safety | Strategy |
|---|---|---|
| Add field with default | Safe | lazy default injection |
| Remove unused field | Safe | payload cleanup |
| Rename field | Unsafe | dynamic key remap |
| Change field type | Unsafe | registered coercion function |
| Add required field (no default) | Unsafe | block with structured error or fallback |
| Interrupted thread on deleted node | Unsafe | `NodeRemap` inside a migration |
