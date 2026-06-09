"""LangMigrate command line interface (Typer).

Commands that only touch revision files (``init``, ``revision``, ``history``,
``check``, ``current``) work offline. ``upgrade``, ``downgrade``, ``stamp`` and
``current --db`` build an adapter from ``langmigrate.toml`` and need a database.
"""

from __future__ import annotations

import re
import string
from datetime import datetime, timezone
from pathlib import Path

import typer

from ..config import DEFAULT_CONFIG_FILE, DEFAULT_CONFIG_TOML, LangMigrateConfig
from ..core.engine import HEAD, MigrationEngine
from ..core.exceptions import LangMigrateError, MultipleHeadsError
from ..core.registry import MigrationRegistry, new_revision_id

app = typer.Typer(
    name="langmigrate",
    help="Declarative schema migrations for LangGraph state persistence.",
    no_args_is_help=True,
    add_completion=False,
)

_TEMPLATE = Path(__file__).parent / "templates" / "revision.py.tmpl"
_AUTO_TEMPLATE = Path(__file__).parent / "templates" / "revision_auto.py.tmpl"

_MIGRATIONS_README = """\
# Migrations

Revision scripts for [LangMigrate](https://github.com/) live here. Each file is one
Alembic-style revision, chained to its parent through `down_revision`.

## Workflow

```bash
langmigrate revision -m "add context field"      # new empty revision
langmigrate revision -m "add field" \\
    --autogenerate --schema myapp.state:AgentState   # diff a schema to fill the body
langmigrate history                               # show the revision DAG
langmigrate check                                 # multiple heads / irreversible revisions
langmigrate upgrade head                          # migrate the database (batch)
```

Migrations must be **pure** and **idempotent**: no I/O, no clocks, and re-applying an
already-migrated state is a no-op. Every `upgrade` should have a matching `downgrade`
(or call `self.raise_irreversible()`).

This `{dir}/` directory is a regular Python package (`__init__.py`); revision files are
discovered by filename, and files starting with `_` are ignored.
"""


def _slugify(message: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")
    return slug or "revision"


def _load_registry(cfg: LangMigrateConfig) -> MigrationRegistry:
    if not cfg.migrations_path.is_dir():
        typer.secho(
            f"No migrations directory at {cfg.migrations_path}. Run `langmigrate init`.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return MigrationRegistry.from_path(cfg.migrations_path)


def _build_adapter(cfg: LangMigrateConfig):
    if cfg.backend not in ("postgres", "redis"):
        typer.secho(
            f"Unknown backend {cfg.backend!r} (expected 'postgres' or 'redis').",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if not cfg.url:
        typer.secho(
            "No database url configured (set it in langmigrate.toml or LANGMIGRATE_URL).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if cfg.backend == "redis":
        from ..adapters.redis import RedisAdapter

        return RedisAdapter.from_conn_string(cfg.url)
    from ..adapters.postgres import PostgresAdapter

    return PostgresAdapter.from_conn_string(cfg.url)


def _scaffold_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` only if it does not already exist."""
    if path.exists():
        typer.secho(f"  {path} exists; left untouched.", fg=typer.colors.YELLOW)
        return
    path.write_text(content)
    typer.secho(f"  Created {path}", fg=typer.colors.GREEN)


@app.command()
def init(
    migrations_dir: str = typer.Option("migrations", help="Directory for revision scripts."),
    example: bool = typer.Option(
        False, "--example", help="Also scaffold a first (empty) revision skeleton."
    ),
) -> None:
    """Create langmigrate.toml and a scaffolded migrations directory."""
    config_path = Path(DEFAULT_CONFIG_FILE)
    if config_path.exists():
        typer.secho(
            f"{DEFAULT_CONFIG_FILE} already exists; leaving it untouched.",
            fg=typer.colors.YELLOW,
        )
    else:
        config_path.write_text(DEFAULT_CONFIG_TOML.replace('"migrations"', f'"{migrations_dir}"'))
        typer.secho(f"Created {DEFAULT_CONFIG_FILE}", fg=typer.colors.GREEN)

    mig_dir = Path(migrations_dir)
    mig_dir.mkdir(parents=True, exist_ok=True)
    typer.secho(f"Created {mig_dir}/", fg=typer.colors.GREEN)
    # `__init__.py` makes the directory importable; it is skipped by discovery
    # (files starting with `_` are ignored), so it stays empty.
    _scaffold_file(mig_dir / "__init__.py", "")
    _scaffold_file(mig_dir / "README.md", _MIGRATIONS_README.format(dir=migrations_dir))

    if example:
        _scaffold_example_revision(mig_dir)

    typer.echo('Next: `langmigrate revision -m "your first change"`')


def _scaffold_example_revision(mig_dir: Path) -> None:
    revision_id = new_revision_id()
    slug = "initial"
    rendered = string.Template(_TEMPLATE.read_text()).substitute(
        message="initial revision",
        revision=revision_id,
        down_revision="(base)",
        down_revision_repr=repr(None),
        slug=slug,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    _scaffold_file(mig_dir / f"{revision_id}_{slug}.py", rendered)


@app.command()
def revision(
    message: str = typer.Option(..., "-m", "--message", help="Description of the change."),
    autogenerate: bool = typer.Option(
        False, "--autogenerate", help="Diff a schema against the head snapshot to fill the body."
    ),
    schema: str = typer.Option(
        None, "--schema", help="Schema ref 'module.path:Attr' (required with --autogenerate)."
    ),
) -> None:
    """Generate a new revision script chained onto the current head."""
    cfg = LangMigrateConfig.load()
    cfg.migrations_path.mkdir(parents=True, exist_ok=True)
    registry = MigrationRegistry.from_path(cfg.migrations_path)

    try:
        down_revision = registry.head() if len(registry) else None
    except MultipleHeadsError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    revision_id = new_revision_id()
    slug = _slugify(message)
    common = {
        "message": message,
        "revision": revision_id,
        "down_revision": down_revision or "(base)",
        "down_revision_repr": repr(down_revision),
        "slug": slug,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if autogenerate:
        rendered = _render_autogenerated(common, registry, down_revision, schema)
    else:
        rendered = string.Template(_TEMPLATE.read_text()).substitute(**common)

    out = cfg.migrations_path / f"{revision_id}_{slug}.py"
    out.write_text(rendered)
    typer.secho(f"Created revision {revision_id} -> {out}", fg=typer.colors.GREEN)
    if down_revision:
        typer.echo(f"  down_revision = {down_revision}")


def _render_autogenerated(common, registry, down_revision, schema) -> str:
    from ..core.schema import diff_schema, load_schema, render_bodies

    if not schema:
        typer.secho("--autogenerate requires --schema 'module.path:Attr'.", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        new_schema = load_schema(schema)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        typer.secho(f"Could not load schema {schema!r}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    old_schema = registry.get(down_revision).fields or {} if down_revision else {}
    diff = diff_schema(old_schema, new_schema)
    if diff.is_empty:
        typer.secho("No schema changes detected against the head snapshot.", fg=typer.colors.YELLOW)
    else:
        summary = f"+{len(diff.added)} -{len(diff.removed)} ~{len(diff.changed)}"
        typer.echo(f"  autogenerated: {summary} (added/removed/changed)")

    up_lines, down_lines = render_bodies(diff)
    indent = " " * 8
    return string.Template(_AUTO_TEMPLATE.read_text()).substitute(
        **common,
        fields_repr=repr(new_schema),
        upgrade_body="\n".join(indent + line for line in up_lines),
        downgrade_body="\n".join(indent + line for line in down_lines),
    )


@app.command()
def history() -> None:
    """Print the revision DAG, newest first per head."""
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)
    if not len(registry):
        typer.echo("(no revisions yet)")
        return
    for head in registry.heads():
        for rev in reversed(registry.lineage(head)):
            mig = registry.get(rev)
            down = mig.down_revision or "(base)"
            marker = " (head)" if rev == head else ""
            typer.echo(f"{rev} <- {down}  {mig.slug}{marker}")


@app.command()
def current(
    db: bool = typer.Option(
        False, "--db", help="Also show the revision distribution in the database."
    ),
) -> None:
    """Show the code's head revision (and optionally the database state)."""
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)
    try:
        typer.secho(f"code head: {registry.head()}", fg=typer.colors.GREEN)
    except MultipleHeadsError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW)

    if db:
        adapter = _build_adapter(cfg)
        try:
            counts = adapter.revision_counts()
        finally:
            adapter.close()
        typer.echo("database revisions:")
        for rev, count in sorted(counts.items()):
            typer.echo(f"  {rev}: {count}")


@app.command()
def check() -> None:
    """Report multiple heads and irreversible (no-downgrade) migrations."""
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)
    problems = 0

    heads = registry.heads()
    if len(heads) > 1:
        problems += 1
        typer.secho(f"Multiple heads: {', '.join(sorted(heads))}", fg=typer.colors.RED)

    for mig in registry:
        if not mig.is_reversible:
            problems += 1
            typer.secho(
                f"Revision {mig.revision} ({mig.slug}) has no downgrade (irreversible).",
                fg=typer.colors.YELLOW,
            )

    if problems == 0:
        typer.secho("OK: single head, every revision is reversible.", fg=typer.colors.GREEN)
    else:
        raise typer.Exit(1)


@app.command()
def upgrade(
    target: str = typer.Argument(HEAD, help="Target revision (default: head)."),
    online_dry_run: bool = typer.Option(
        False, "--online-dry-run", help="Count stale checkpoints without writing."
    ),
) -> None:
    """Proactively migrate every stale checkpoint in the database up to TARGET."""
    from ..runtime.batch import run_batch_upgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_registry(cfg))
    adapter = _build_adapter(cfg)
    try:
        adapter.setup()
        result = run_batch_upgrade(adapter, engine, target=target, dry_run=online_dry_run)
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    typer.secho(str(result), fg=typer.colors.GREEN)


@app.command()
def downgrade(
    target: str = typer.Argument(..., help="Target revision to downgrade to ('base' for none)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count without writing."),
) -> None:
    """Downgrade every checkpoint in the database down to TARGET."""
    from ..runtime.batch import run_batch_downgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_registry(cfg))
    resolved: str | None = None if target == "base" else target
    adapter = _build_adapter(cfg)
    try:
        adapter.setup()
        result = run_batch_downgrade(adapter, engine, resolved, dry_run=dry_run)
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    typer.secho(str(result), fg=typer.colors.GREEN)


@app.command()
def stamp(
    revision: str = typer.Argument(..., help="Revision id to record on all checkpoints."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Set the revision tag on all checkpoints WITHOUT running migrations.

    Use only when the stored data already matches REVISION (e.g. adopting LangMigrate
    on an existing DB). Stamping data that does NOT match silently marks it migrated:
    the engine will then treat it as up to date and never transform it.
    """
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)
    registry.get(revision)  # validate it exists

    typer.secho(
        f"WARNING: stamp tags every checkpoint as {revision!r} WITHOUT migrating data. "
        "If the stored data doesn't already match this revision, it will be treated as "
        "up to date and never upgraded. Use `upgrade` to actually transform data.",
        fg=typer.colors.YELLOW,
    )
    if not yes:
        typer.confirm("Proceed with stamping?", abort=True)

    adapter = _build_adapter(cfg)
    try:
        adapter.setup()
        updated = adapter.stamp_all(revision)
    finally:
        adapter.close()
    typer.secho(f"Stamped {updated} checkpoints as {revision}.", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
