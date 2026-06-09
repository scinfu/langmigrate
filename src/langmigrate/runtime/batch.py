"""Proactive (batch) migration runner.

Reuses the lazy :class:`MigrationInterceptor` so the *same* upgrade + idempotent
write-back logic powers both online and offline migration. The adapter supplies
the efficient "which checkpoints are stale" enumeration; the interceptor does the
actual transform on each one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.base import BatchCheckpointAdapter, CheckpointAdapter
from ..core.engine import HEAD, MigrationEngine
from ..core.version import envelope_from_parts
from .interceptor import MigrationInterceptor
from .persistence import build_migrated_tuple, changed_versions, put_config


@dataclass
class BatchResult:
    """Summary of a batch migration run.

    ``total`` is the number of checkpoints this run *considered*: for an upgrade
    that is the stale count; for a downgrade it is the number of checkpoints
    scanned. ``migrated`` is how many were actually changed.
    """

    target: str
    total: int
    migrated: int
    dry_run: bool

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        verb = "would migrate" if self.dry_run else "migrated"
        return f"{verb} {self.migrated}/{self.total} checkpoints to {self.target}"


def run_batch_upgrade(
    adapter: CheckpointAdapter,
    engine: MigrationEngine,
    *,
    target: str = HEAD,
    dry_run: bool = False,
) -> BatchResult:
    """Upgrade every stale checkpoint exposed by ``adapter`` to ``target``.

    With ``dry_run`` the database is never written: stale checkpoints are counted
    but not modified (write-back is disabled).
    """
    head = engine.resolve_target(target)
    interceptor = MigrationInterceptor(adapter.saver, engine, write_back=not dry_run, target=target)

    total = adapter.count_stale(head)
    migrated = 0
    for config in adapter.iter_stale_configs(head):
        if dry_run:
            migrated += 1
            continue
        # get_tuple triggers the lazy cascade + idempotent write-back.
        interceptor.get_tuple(config)
        migrated += 1
    return BatchResult(target=head, total=total, migrated=migrated, dry_run=dry_run)


def run_batch_downgrade(
    adapter: BatchCheckpointAdapter,
    engine: MigrationEngine,
    target: str | None,
    *,
    dry_run: bool = False,
) -> BatchResult:
    """Downgrade every checkpoint down to ``target`` (``None`` = past the base).

    Requires the adapter to enumerate *all* checkpoints (``iter_all_configs``), not
    just stale ones, since a downgrade target is below the current head.
    """
    saver = adapter.saver
    resolved = "base" if target is None else engine.resolve_target(target)
    migrated = 0
    total = 0
    for config in adapter.iter_all_configs():
        tup = saver.get_tuple(config)
        if tup is None:
            continue
        total += 1
        envelope = envelope_from_parts(tup.checkpoint["channel_values"], dict(tup.metadata or {}))
        if envelope.revision is None:
            continue
        new_env = engine.downgrade_state(envelope, target)
        if new_env is envelope:
            continue
        migrated += 1
        if dry_run:
            continue
        new_tuple = build_migrated_tuple(tup, new_env, saver)
        saver.put(
            put_config(new_tuple),
            new_tuple.checkpoint,
            new_tuple.metadata,
            changed_versions(tup.checkpoint, new_tuple.checkpoint),
        )
    return BatchResult(target=resolved, total=total, migrated=migrated, dry_run=dry_run)
