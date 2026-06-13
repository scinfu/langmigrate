"""Proactive (batch) migration runner.

Shares the *same* upgrade + idempotent write-back mechanics as the lazy
interceptor via :mod:`langmigrate.runtime.persistence`. The adapter supplies the
efficient "which checkpoints are stale" enumeration; the runner transforms each
one, collecting per-checkpoint failures when ``continue_on_error`` is set.

``dry_run`` executes the full cascade **in memory** (so it validates the
migrations against real data) and only skips the write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, CheckpointTuple

from ..adapters.base import (
    AsyncBatchCheckpointAdapter,
    AsyncCheckpointAdapter,
    BatchCheckpointAdapter,
    CheckpointAdapter,
    StoreAdapter,
)
from ..core.engine import HEAD, MigrationEngine
from ..core.exceptions import RevisionNotFoundError
from ..core.types import OnUnknownRevision, StateEnvelope
from ..core.version import envelope_from_item_parts, envelope_from_parts, value_for
from .persistence import build_migrated_tuple, changed_versions, put_config

logger = logging.getLogger("langmigrate.runtime.batch")


@dataclass
class BatchFailure:
    """A single checkpoint the batch runner could not migrate."""

    ref: str  # "thread_id/checkpoint_ns/checkpoint_id"
    error: str
    error_type: str


@dataclass
class BatchResult:
    """Summary of a batch migration run.

    ``total`` is the number of checkpoints this run *considered* (enumerated as
    stale for an upgrade; scanned for a downgrade). ``migrated`` is how many were
    actually changed (or would be, under ``dry_run``). ``failures`` is populated
    only when running with ``continue_on_error``.
    """

    target: str
    total: int
    migrated: int
    dry_run: bool
    failed: int = 0
    failures: list[BatchFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the run completed without per-checkpoint failures."""
        return self.failed == 0

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        verb = "would migrate" if self.dry_run else "migrated"
        suffix = f" ({self.failed} failed)" if self.failed else ""
        return f"{verb} {self.migrated}/{self.total} checkpoints to {self.target}{suffix}"


def _config_ref(config: RunnableConfig) -> str:
    cfg = config.get("configurable", {})
    return "/".join(
        str(cfg.get(key, "")) for key in ("thread_id", "checkpoint_ns", "checkpoint_id")
    )


def _tolerate_unknown_revision(
    exc: RevisionNotFoundError,
    envelope: StateEnvelope,
    on_unknown_revision: OnUnknownRevision,
    ref: str,
) -> bool:
    """Whether to skip an item whose own revision tag the registry doesn't know.

    Mirrors the lazy paths' ``on_unknown_revision`` policy (see
    :class:`~langmigrate.runtime.interceptor.MigrationInterceptor`): the tolerance
    applies ONLY to the item's own tag — the code-rollback case, where the stored
    revision is simply ahead of / unknown to the rolled-back code and there is
    nothing to migrate it to. A *different* unknown revision (a bad target or a
    broken registry pointer) always re-raises. Returns ``True`` when the item
    should be skipped, ``False`` when the error must propagate.
    """
    if on_unknown_revision == "raise" or exc.revision != envelope.revision:
        return False
    if on_unknown_revision == "warn":
        logger.warning(
            "langmigrate: %s carries unknown revision %r (not in the registry); "
            "skipping it. This usually means the code was rolled back after a lazy "
            "migration.",
            ref,
            exc.revision,
        )
    return True


def _plan_upgrade(
    tup: CheckpointTuple,
    engine: MigrationEngine,
    target_rev: str,
    saver: BaseCheckpointSaver,
    on_unknown_revision: OnUnknownRevision,
    ref: str,
) -> tuple[CheckpointTuple, ChannelVersions] | None:
    """Pure per-checkpoint upgrade step: ``None`` if already at target or skipped.

    Returns the rebuilt tuple and the channel versions to (re)write. Shared by the
    sync and async runners so their semantics cannot drift. An item whose own tag
    is unknown to the registry is skipped (``None``) or re-raised per
    ``on_unknown_revision``.
    """
    envelope = envelope_from_parts(tup.checkpoint["channel_values"], dict(tup.metadata or {}))
    try:
        new_env = engine.upgrade_state(envelope, target_rev)
    except RevisionNotFoundError as exc:
        if _tolerate_unknown_revision(exc, envelope, on_unknown_revision, ref):
            return None
        raise
    if new_env is envelope:
        return None
    new_tuple = build_migrated_tuple(tup, new_env, saver)
    return new_tuple, changed_versions(tup.checkpoint, new_tuple.checkpoint)


def _plan_downgrade(
    tup: CheckpointTuple,
    engine: MigrationEngine,
    target: str | None,
    saver: BaseCheckpointSaver,
    on_unknown_revision: OnUnknownRevision,
    ref: str,
) -> tuple[CheckpointTuple, ChannelVersions] | None:
    """Pure per-checkpoint downgrade step: ``None`` if untagged, at target or skipped."""
    envelope = envelope_from_parts(tup.checkpoint["channel_values"], dict(tup.metadata or {}))
    if envelope.revision is None:
        return None
    try:
        new_env = engine.downgrade_state(envelope, target)
    except RevisionNotFoundError as exc:
        if _tolerate_unknown_revision(exc, envelope, on_unknown_revision, ref):
            return None
        raise
    if new_env is envelope:
        return None
    new_tuple = build_migrated_tuple(tup, new_env, saver)
    return new_tuple, changed_versions(tup.checkpoint, new_tuple.checkpoint)


def run_batch_upgrade(
    adapter: CheckpointAdapter,
    engine: MigrationEngine,
    *,
    target: str = HEAD,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Upgrade every stale checkpoint exposed by ``adapter`` to ``target``.

    ``dry_run`` runs the full cascade in memory — validating every migration
    against the real data — but never writes. With ``continue_on_error`` a failing
    checkpoint is recorded in :attr:`BatchResult.failures` instead of aborting the
    run (deserialization and migration errors alike).

    ``on_unknown_revision`` mirrors the lazy interceptor's policy for a checkpoint
    whose own tag the registry does not know (a code rollback after a lazy
    migration leaves it tagged ahead of the rolled-back code): ``"raise"``
    (default) fails the run, ``"warn"``/``"pass"`` skip it (it is counted in
    ``total`` but not migrated).
    """
    head = engine.resolve_target(target)
    saver = adapter.saver
    total = migrated = 0
    failures: list[BatchFailure] = []
    for config in adapter.iter_stale_configs(head):
        total += 1
        ref = _config_ref(config)
        try:
            tup = saver.get_tuple(config)
            if tup is None:
                continue
            plan = _plan_upgrade(tup, engine, head, saver, on_unknown_revision, ref)
            if plan is None:
                continue
            migrated += 1
            if dry_run:
                continue
            new_tuple, versions = plan
            saver.put(put_config(new_tuple), new_tuple.checkpoint, new_tuple.metadata, versions)
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=head,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )


def run_batch_downgrade(
    adapter: BatchCheckpointAdapter,
    engine: MigrationEngine,
    target: str | None,
    *,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Downgrade every checkpoint down to ``target`` (``None`` = past the base).

    Requires the adapter to enumerate *all* checkpoints (``iter_all_configs``), not
    just stale ones, since a downgrade target is below the current head. ``dry_run``,
    ``continue_on_error`` and ``on_unknown_revision`` behave as in
    :func:`run_batch_upgrade`.
    """
    saver = adapter.saver
    resolved = "base" if target is None else engine.resolve_target(target)
    total = migrated = 0
    failures: list[BatchFailure] = []
    for config in adapter.iter_all_configs():
        total += 1
        ref = _config_ref(config)
        try:
            tup = saver.get_tuple(config)
            if tup is None:
                continue
            plan = _plan_downgrade(tup, engine, target, saver, on_unknown_revision, ref)
            if plan is None:
                continue
            migrated += 1
            if dry_run:
                continue
            new_tuple, versions = plan
            saver.put(put_config(new_tuple), new_tuple.checkpoint, new_tuple.metadata, versions)
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=resolved,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )


async def arun_batch_upgrade(
    adapter: AsyncCheckpointAdapter,
    engine: MigrationEngine,
    *,
    target: str = HEAD,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Async counterpart of :func:`run_batch_upgrade` (same semantics)."""
    head = engine.resolve_target(target)
    saver = adapter.saver
    total = migrated = 0
    failures: list[BatchFailure] = []
    async for config in adapter.aiter_stale_configs(head):
        total += 1
        ref = _config_ref(config)
        try:
            tup = await saver.aget_tuple(config)
            if tup is None:
                continue
            plan = _plan_upgrade(tup, engine, head, saver, on_unknown_revision, ref)
            if plan is None:
                continue
            migrated += 1
            if dry_run:
                continue
            new_tuple, versions = plan
            await saver.aput(
                put_config(new_tuple), new_tuple.checkpoint, new_tuple.metadata, versions
            )
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=head,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )


async def arun_batch_downgrade(
    adapter: AsyncBatchCheckpointAdapter,
    engine: MigrationEngine,
    target: str | None,
    *,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Async counterpart of :func:`run_batch_downgrade` (same semantics)."""
    saver = adapter.saver
    resolved = "base" if target is None else engine.resolve_target(target)
    total = migrated = 0
    failures: list[BatchFailure] = []
    async for config in adapter.aiter_all_configs():
        total += 1
        ref = _config_ref(config)
        try:
            tup = await saver.aget_tuple(config)
            if tup is None:
                continue
            plan = _plan_downgrade(tup, engine, target, saver, on_unknown_revision, ref)
            if plan is None:
                continue
            migrated += 1
            if dry_run:
                continue
            new_tuple, versions = plan
            await saver.aput(
                put_config(new_tuple), new_tuple.checkpoint, new_tuple.metadata, versions
            )
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=resolved,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )


# -- store batch runners -------------------------------------------------------


def _item_ref(namespace: tuple[str, ...], key: str) -> str:
    return "/".join(namespace) + ":" + key


def run_store_batch_upgrade(
    adapter: StoreAdapter,
    engine: MigrationEngine,
    *,
    target: str = HEAD,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Upgrade every stale store item exposed by ``adapter`` to ``target``.

    Same semantics as :func:`run_batch_upgrade`: ``dry_run`` validates the cascade
    in memory; ``continue_on_error`` records failures instead of aborting;
    ``on_unknown_revision`` skips (``"warn"``/``"pass"``) or raises (``"raise"``,
    default) on an item whose own tag the registry does not know.
    """
    head = engine.resolve_target(target)
    store = adapter.store
    total = migrated = 0
    failures: list[BatchFailure] = []
    for namespace, key in adapter.iter_stale_items(head):
        total += 1
        ref = _item_ref(namespace, key)
        try:
            item = store.get(namespace, key)
            if item is None:
                continue
            # ``value=None`` (possible with external/custom stores) is never
            # tagged nor migrated — see MigrationStore._migrate_item. We still
            # count it in ``total`` (it was enumerated as stale) but skip the
            # upgrade.
            if item.value is None:
                continue
            envelope = envelope_from_item_parts(item.value, namespace=namespace, key=key)
            try:
                new_env = engine.upgrade_state(envelope, head)
            except RevisionNotFoundError as exc:
                if _tolerate_unknown_revision(exc, envelope, on_unknown_revision, ref):
                    continue
                raise
            if new_env is envelope:
                continue
            migrated += 1
            if dry_run:
                continue
            store.put(namespace, key, value_for(new_env))
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=head,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )


def run_store_batch_downgrade(
    adapter: StoreAdapter,
    engine: MigrationEngine,
    target: str | None,
    *,
    dry_run: bool = False,
    continue_on_error: bool = False,
    on_unknown_revision: OnUnknownRevision = "raise",
) -> BatchResult:
    """Downgrade every store item down to ``target`` (``None`` = past the base)."""
    store = adapter.store
    resolved = "base" if target is None else engine.resolve_target(target)
    total = migrated = 0
    failures: list[BatchFailure] = []
    for namespace, key in adapter.iter_all_items():
        total += 1
        ref = _item_ref(namespace, key)
        try:
            item = store.get(namespace, key)
            if item is None:
                continue
            if item.value is None:
                continue
            envelope = envelope_from_item_parts(item.value, namespace=namespace, key=key)
            if envelope.revision is None:
                continue
            try:
                new_env = engine.downgrade_state(envelope, target)
            except RevisionNotFoundError as exc:
                if _tolerate_unknown_revision(exc, envelope, on_unknown_revision, ref):
                    continue
                raise
            if new_env is envelope:
                continue
            migrated += 1
            if dry_run:
                continue
            store.put(namespace, key, value_for(new_env))
        except Exception as exc:
            if not continue_on_error:
                raise
            failures.append(BatchFailure(ref=ref, error=str(exc), error_type=type(exc).__name__))
    return BatchResult(
        target=resolved,
        total=total,
        migrated=migrated,
        dry_run=dry_run,
        failed=len(failures),
        failures=failures,
    )
