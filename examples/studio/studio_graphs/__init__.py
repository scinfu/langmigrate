"""LangGraph Studio examples for LangMigrate.

Three graphs, one per Studio-compatible migration path:

- ``chat``   — a dedicated ``migrate`` node calling ``migrate_state_update``.
- ``agent``  — ``create_agent`` + ``SchemaMigrationMiddleware``.
- ``memory`` — store items healed by ``MigrationStore`` (``setup_langmigrate_store``).

Each graph carries ``SCHEMA_VERSION`` / ``LANGMIGRATE_ENABLED`` toggles at the top of
its ``graph.py`` so you can break old threads on purpose and then heal them, live in
Studio. See ``examples/studio/README.md`` for the walkthrough.
"""
