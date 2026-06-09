# LangMigrate — Examples

Each subdirectory is a self-contained, runnable example. All use `InMemorySaver` so
they work with **no Docker required** unless noted otherwise.

```bash
uv run python examples/<name>/demo.py     # most examples
uv run python examples/quickstart/main.py # the quickstart
```

## At a glance

| Example | Pattern | Key features |
|---|---|---|
| [`quickstart`](quickstart/) | Online lazy (one-liner) | `setup_langmigrate` + `@migration` decorator; type-checked with `mypy --strict` |
| [`evolving_agent`](evolving_agent/) | Online lazy (saver wrap) | Baseline: MigrationInterceptor + write-back, add/rename/coerce |
| [`middleware_agent`](middleware_agent/) | State-level / managed platform | `migrate_state_update`, `SchemaMigrationMiddleware`, node pattern |
| [`multi_tool_agent`](multi_tool_agent/) | Online lazy (StateGraph) | 3-revision cascade, rename, require_field, type coercion |
| [`deep_research_agent`](deep_research_agent/) | Advanced / supervisor | NodeRemap, IrreversibleMigrationError, staged partial upgrade |
| [`batch_migration`](batch_migration/) | Offline proactive batch | `run_batch_upgrade`, `run_batch_downgrade`, dry-run, InMemoryAdapter |

## Which pattern should I use?

```
Do you own the checkpointer?
├── Yes → MigrationInterceptor (evolving_agent, multi_tool_agent)
│         └── Need bulk pre-release cure? → run_batch_upgrade (batch_migration)
└── No  → LangGraph Server / Cloud?
          ├── Has middleware stack → SchemaMigrationMiddleware (middleware_agent)
          └── Manual graph control → migrate_state_update node (middleware_agent)
```

Special cases:
- **Node was renamed in the graph** → add `remap_node` in the first migration after the rename (deep_research_agent)
- **Migration must not be rolled back** → call `self.raise_irreversible()` in `downgrade` (deep_research_agent)
- **Staged rollout / A/B** → `MigrationInterceptor(..., target="<intermediate-rev>")` (deep_research_agent)
