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
from rich.console import Console
from rich.table import Table
from rich.text import Text

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

store_app = typer.Typer(
    name="store",
    help="Schema migrations for the LangGraph BaseStore (long-term memory items).",
    no_args_is_help=True,
)
app.add_typer(store_app, name="store")

_TEMPLATE = Path(__file__).parent / "templates" / "revision.py.tmpl"
_AUTO_TEMPLATE = Path(__file__).parent / "templates" / "revision_auto.py.tmpl"
_MERGE_TEMPLATE = Path(__file__).parent / "templates" / "merge.py.tmpl"

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
langmigrate merge -m "join heads"                 # merge branched heads into one
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
    return _registry_from_path(cfg.migrations_path)


def _load_store_registry(cfg: LangMigrateConfig) -> MigrationRegistry:
    if not cfg.store_migrations_path.is_dir():
        typer.secho(
            f"No store migrations directory at {cfg.store_migrations_path}. "
            "Run `langmigrate init --with-store`.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return _registry_from_path(cfg.store_migrations_path)


def _registry_from_path(path: Path) -> MigrationRegistry:
    """Build a registry, rendering registry errors (duplicates, cycles, ...) nicely."""
    try:
        return MigrationRegistry.from_path(path)
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc


def _require_revision(registry: MigrationRegistry, revision: str) -> None:
    """Validate ``revision`` exists, rendering the error nicely instead of a traceback."""
    try:
        registry.get(revision)
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc


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


def _build_store_adapter(cfg: LangMigrateConfig):
    if cfg.backend == "redis":
        typer.secho(
            "Store batch migration is Postgres-only for now (the online MigrationStore "
            "wrapper works with any backend).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    if cfg.backend != "postgres":
        typer.secho(f"Unknown backend {cfg.backend!r} (expected 'postgres').", fg=typer.colors.RED)
        raise typer.Exit(1)
    if not cfg.url:
        typer.secho(
            "No database url configured (set it in langmigrate.toml or LANGMIGRATE_URL).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    from ..adapters.postgres import PostgresStoreAdapter

    return PostgresStoreAdapter.from_conn_string(cfg.url)


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
    with_store: bool = typer.Option(
        False, "--with-store", help="Also scaffold a store_migrations directory (BaseStore)."
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

    if with_store:
        store_dir = Path(LangMigrateConfig().store_migrations_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        typer.secho(f"Created {store_dir}/", fg=typer.colors.GREEN)
        _scaffold_file(store_dir / "__init__.py", "")

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


def _create_revision(
    migrations_path: Path,
    message: str,
    autogenerate: bool,
    schema: str | None,
    *,
    merge_hint: str = 'langmigrate merge -m "merge heads"',
) -> None:
    """Shared body of ``revision`` / ``store revision``."""
    migrations_path.mkdir(parents=True, exist_ok=True)
    registry = _registry_from_path(migrations_path)

    try:
        down_revision = registry.head() if len(registry) else None
    except MultipleHeadsError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        typer.secho(f"Run: {merge_hint}", fg=typer.colors.YELLOW)
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

    out = migrations_path / f"{revision_id}_{slug}.py"
    out.write_text(rendered)
    typer.secho(f"Created revision {revision_id} -> {out}", fg=typer.colors.GREEN)
    if down_revision:
        typer.echo(f"  down_revision = {down_revision}")


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
    _create_revision(cfg.migrations_path, message, autogenerate, schema)


def _baseline_fields(registry: MigrationRegistry, down_revision: str | None) -> dict[str, str]:
    """Most recent ``fields`` snapshot at or below ``down_revision``.

    Merge revisions (and hand-written ones) carry ``fields = None``, so walk the
    lineage newest-first until a snapshot is found.
    """
    if down_revision is None:
        return {}
    for rev in reversed(registry.lineage(down_revision)):
        fields = registry.get(rev).fields
        if fields is not None:
            return fields
    return {}


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

    old_schema = _baseline_fields(registry, down_revision)
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


def _format_parents(mig) -> str:
    parents = mig.parents
    if not parents:
        return "(base)"
    return " + ".join(parents)


@app.command()
def merge(
    revisions: list[str] = typer.Argument(
        None, help="Revisions to merge. Omit (or pass 'heads') to merge all current heads."
    ),
    message: str = typer.Option(..., "-m", "--message", help="Description of the merge."),
) -> None:
    """Create a merge revision joining multiple heads into a single one."""
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)

    if not revisions or revisions == ["heads"]:
        parents = sorted(registry.heads())
    else:
        parents = sorted(set(revisions))
    if len(parents) < 2:
        typer.secho(
            f"Nothing to merge: need at least two distinct revisions, got {parents}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    for rev in parents:
        if rev not in registry:
            typer.secho(f"Revision {rev!r} not found in the registry.", fg=typer.colors.RED)
            raise typer.Exit(1)
    for rev in parents:
        ancestors = registry.ancestors(rev)
        overlapping = [other for other in parents if other != rev and other in ancestors]
        if overlapping:
            typer.secho(
                f"Cannot merge {overlapping[0]!r} with its own descendant {rev!r}: "
                "the edge would be redundant.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)

    revision_id = new_revision_id()
    slug = _slugify(message)
    rendered = string.Template(_MERGE_TEMPLATE.read_text()).substitute(
        message=message,
        revision=revision_id,
        parents_label=", ".join(parents),
        down_revision_repr=repr(tuple(parents)),
        slug=slug,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    out = cfg.migrations_path / f"{revision_id}_{slug}.py"
    out.write_text(rendered)
    typer.secho(f"Created merge revision {revision_id} -> {out}", fg=typer.colors.GREEN)
    typer.echo(f"  down_revision = {tuple(parents)!r}")


def _emit_history(registry: MigrationRegistry) -> None:
    if not len(registry):
        typer.echo("(no revisions yet)")
        return

    console = Console()
    table = Table(box=None, padding=(0, 2))
    table.add_column("Revision", style="cyan")
    table.add_column("Parents", style="magenta")
    table.add_column("Description")

    printed: set[str] = set()
    heads = set(registry.heads())
    for head in sorted(heads):
        for rev in reversed(registry.lineage(head)):
            if rev in printed:
                continue
            printed.add(rev)
            mig = registry.get(rev)

            rev_display = Text(f"{rev} (head)", style="bold green") if rev in heads else Text(rev)

            # Text cells: slugs are arbitrary strings and must not be parsed as markup.
            table.add_row(rev_display, Text(_format_parents(mig)), Text(mig.slug))

    console.print(table)


def _emit_current(registry: MigrationRegistry, adapter) -> None:
    """Print the code head; with ``adapter`` also the DB revision distribution."""
    try:
        typer.secho(f"code head: {registry.head()}", fg=typer.colors.GREEN)
    except MultipleHeadsError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW)

    if adapter is not None:
        try:
            counts = adapter.revision_counts()
        finally:
            adapter.close()
        typer.echo("database revisions:")
        for rev, count in sorted(counts.items()):
            typer.echo(f"  {rev}: {count}")


def _emit_check(registry: MigrationRegistry) -> None:
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
def history() -> None:
    """Print the revision DAG, newest first per head."""
    cfg = LangMigrateConfig.load()
    _emit_history(_load_registry(cfg))


@app.command()
def current(
    db: bool = typer.Option(
        False, "--db", help="Also show the revision distribution in the database."
    ),
) -> None:
    """Show the code's head revision (and optionally the database state)."""
    cfg = LangMigrateConfig.load()
    registry = _load_registry(cfg)
    _emit_current(registry, _build_adapter(cfg) if db else None)


@app.command()
def check() -> None:
    """Report multiple heads and irreversible (no-downgrade) migrations."""
    cfg = LangMigrateConfig.load()
    _emit_check(_load_registry(cfg))


def _report_batch_result(result) -> None:
    """Render a BatchResult, listing failures and exiting non-zero if any."""
    color = typer.colors.GREEN if result.ok else typer.colors.YELLOW
    typer.secho(str(result), fg=color)
    if result.ok:
        return
    shown = result.failures[:20]
    for failure in shown:
        typer.secho(f"  {failure.ref}: {failure.error_type}: {failure.error}", fg=typer.colors.RED)
    remaining = result.failed - len(shown)
    if remaining > 0:
        typer.secho(f"  ... and {remaining} more failures", fg=typer.colors.RED)
    raise typer.Exit(1)


@app.command()
def upgrade(
    target: str = typer.Argument(HEAD, help="Target revision (default: head)."),
    online_dry_run: bool = typer.Option(
        False,
        "--online-dry-run",
        help="Run the full cascade in memory without writing (validates migrations).",
    ),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="Record failing checkpoints instead of aborting."
    ),
) -> None:
    """Proactively migrate every stale checkpoint in the database up to TARGET."""
    from ..runtime.batch import run_batch_upgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_registry(cfg))
    adapter = _build_adapter(cfg)
    try:
        adapter.setup()
        result = run_batch_upgrade(
            adapter,
            engine,
            target=target,
            dry_run=online_dry_run,
            continue_on_error=continue_on_error,
        )
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    _report_batch_result(result)


@app.command()
def downgrade(
    target: str = typer.Argument(..., help="Target revision to downgrade to ('base' for none)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the full cascade in memory without writing."
    ),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="Record failing checkpoints instead of aborting."
    ),
) -> None:
    """Downgrade every checkpoint in the database down to TARGET."""
    from ..runtime.batch import run_batch_downgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_registry(cfg))
    resolved: str | None = None if target == "base" else target
    adapter = _build_adapter(cfg)
    try:
        adapter.setup()
        result = run_batch_downgrade(
            adapter, engine, resolved, dry_run=dry_run, continue_on_error=continue_on_error
        )
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    _report_batch_result(result)


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
    _require_revision(registry, revision)  # validate it exists

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


# -- store sub-commands (langmigrate store ...) --------------------------------


@store_app.command("revision")
def store_revision(
    message: str = typer.Option(..., "-m", "--message", help="Description of the change."),
) -> None:
    """Generate a new store revision script chained onto the current store head."""
    cfg = LangMigrateConfig.load()
    _create_revision(
        cfg.store_migrations_path,
        message,
        autogenerate=False,
        schema=None,
        merge_hint="create a merge revision in the store migrations directory",
    )


@store_app.command("history")
def store_history() -> None:
    """Print the store revision DAG, newest first per head."""
    cfg = LangMigrateConfig.load()
    _emit_history(_load_store_registry(cfg))


@store_app.command("current")
def store_current(
    db: bool = typer.Option(
        False, "--db", help="Also show the revision distribution in the database."
    ),
) -> None:
    """Show the store code head (and optionally the database state)."""
    cfg = LangMigrateConfig.load()
    registry = _load_store_registry(cfg)
    _emit_current(registry, _build_store_adapter(cfg) if db else None)


@store_app.command("check")
def store_check() -> None:
    """Report multiple heads and irreversible store migrations."""
    cfg = LangMigrateConfig.load()
    _emit_check(_load_store_registry(cfg))


@store_app.command("upgrade")
def store_upgrade(
    target: str = typer.Argument(HEAD, help="Target revision (default: head)."),
    online_dry_run: bool = typer.Option(
        False,
        "--online-dry-run",
        help="Run the full cascade in memory without writing (validates migrations).",
    ),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="Record failing items instead of aborting."
    ),
) -> None:
    """Proactively migrate every stale store item in the database up to TARGET."""
    from ..runtime.batch import run_store_batch_upgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_store_registry(cfg))
    adapter = _build_store_adapter(cfg)
    try:
        adapter.setup()
        result = run_store_batch_upgrade(
            adapter,
            engine,
            target=target,
            dry_run=online_dry_run,
            continue_on_error=continue_on_error,
        )
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    _report_batch_result(result)


@store_app.command("downgrade")
def store_downgrade(
    target: str = typer.Argument(..., help="Target revision to downgrade to ('base' for none)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the full cascade in memory without writing."
    ),
    continue_on_error: bool = typer.Option(
        False, "--continue-on-error", help="Record failing items instead of aborting."
    ),
) -> None:
    """Downgrade every store item in the database down to TARGET."""
    from ..runtime.batch import run_store_batch_downgrade

    cfg = LangMigrateConfig.load()
    engine = MigrationEngine(_load_store_registry(cfg))
    resolved: str | None = None if target == "base" else target
    adapter = _build_store_adapter(cfg)
    try:
        adapter.setup()
        result = run_store_batch_downgrade(
            adapter, engine, resolved, dry_run=dry_run, continue_on_error=continue_on_error
        )
    except LangMigrateError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    finally:
        adapter.close()
    _report_batch_result(result)


@store_app.command("stamp")
def store_stamp(
    revision: str = typer.Argument(..., help="Revision id to record on all store items."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Set the revision tag on all store items WITHOUT running migrations."""
    cfg = LangMigrateConfig.load()
    registry = _load_store_registry(cfg)
    _require_revision(registry, revision)  # validate it exists

    typer.secho(
        f"WARNING: stamp tags every store item as {revision!r} WITHOUT migrating data. "
        "If the stored data doesn't already match this revision, it will be treated as "
        "up to date and never upgraded. Use `store upgrade` to actually transform data.",
        fg=typer.colors.YELLOW,
    )
    if not yes:
        typer.confirm("Proceed with stamping?", abort=True)

    adapter = _build_store_adapter(cfg)
    try:
        adapter.setup()
        updated = adapter.stamp_all(revision)
    finally:
        adapter.close()
    typer.secho(f"Stamped {updated} store items as {revision}.", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
