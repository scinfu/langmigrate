# Contributing to LangMigrate

Thank you for your interest in contributing! This document explains how to
set up your development environment, run the tests, and submit changes that
fit the project's style and architecture.

## Development setup

LangMigrate uses [uv](https://docs.astral.sh/uv/) as its package manager
and Python 3.10+ as the language floor.

```bash
# Clone and install (all extras for full test coverage)
git clone https://github.com/scinfu/langmigrate.git
cd langmigrate
uv sync --extra dev --extra postgres --extra redis --extra langchain

# Bring up Postgres and Redis for integration tests
docker compose up -d
```

## Architecture

LangMigrate follows **Clean Architecture** strictly:

```
cli/  runtime/  adapters/   ──────►   core/
      (DB clients live here)          (pure: no DB client imports)
```

- `core/` — pure logic. No I/O, no DB drivers. Nothing outside `core/` may
  be imported from within `core/`.
- `adapters/` — DB-specific bulk access for the batch CLI. DB client
  imports are confined here, ideally imported lazily inside methods.
- `runtime/` — `MigrationInterceptor` and batch runners. DB-agnostic;
  delegates to whichever saver it wraps.
- `integrations/` — `SchemaMigrationMiddleware` and pure helpers for
  hand-built `StateGraph`s.
- `cli/` — Typer application.

The dependency rule is non-negotiable: `core` must never import a database
client (`psycopg`, `redis`, …) nor anything from `adapters/`, `runtime/`,
`cli/` or `integrations/`. Keep migration logic independent of any backend.

## Testing

Every feature must have tests. Conventions:

- **Unit tests** (`tests/unit/`): pure logic, no services.
- **Integration tests** (`tests/integration/`): marked with
  `@pytest.mark.integration`; require Docker. They cover end-to-end paths
  on Postgres and Redis.
- Operations, engine, and interceptor changes require coverage of the
  cascade, idempotency, async paths, and the no-op-at-HEAD case.

```bash
uv run pytest                        # unit tests only
uv run pytest -m integration         # integration (needs Docker)
uv run pytest --cov=langmigrate      # with coverage report
```

## Code style

- Python 3.10+, full type hints, Pydantic v2.
- `ruff` for lint and format (line length 100).
- Docstrings on all public APIs (Google style).
- `mypy` is run in CI — keep it happy.

```bash
uv run ruff check . && uv run ruff format .
uv run mypy src/langmigrate
```

## Commit and PR conventions

- Keep commits atomic: one logical change per commit.
- Write imperative commit subjects ("Add feature", not "Added feature").
- PRs must link the issue they address (if any) and summarize the change.
- Run the full unit test suite, `ruff` and `mypy` before pushing.

## Migration authoring rules

Every migration in user code (and the examples in this repository) must:

- Provide both an `upgrade` and a `downgrade`. Genuinely one-way migrations
  raise `IrreversibleMigrationError` from `downgrade`.
- Be **idempotent and pure**: no I/O, no network, no clocks. Re-applying a
  migration to already-migrated state must be a no-op.
- Prefer the declarative helpers (`add_field`, `drop_field`, `rename_field`,
  `coerce_field`, `require_field`) over hand-mutating dicts.
- Never store the version tag in `channel_values` — it lives in
  `checkpoint.metadata` and the framework handles it for you.

## Releasing

Releases are published via PyPI trusted publishing. Pushing a `vX.Y.Z` tag
triggers the `publish.yml` workflow, which builds and uploads the
distribution after running the full test suite. See
[SECURITY.md](./SECURITY.md) for vulnerability reporting.
