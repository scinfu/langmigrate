"""Shared checkpoint rebuild / write-back mechanics.

Both the lazy interceptor and the batch runner need to turn a migrated
:class:`StateEnvelope` back into a ``Checkpoint`` and persist it **idempotently**
(same ``id``, preserved parent chain, versions bumped only for changed channels).
Centralizing it here keeps that subtle logic in one place.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointTuple,
)

from ..core.operations import strict_equal
from ..core.types import StateEnvelope
from ..core.version import metadata_for


def reconcile_versions(
    saver: BaseCheckpointSaver,
    old_versions: dict[str, Any],
    old_values: dict[str, Any],
    new_values: dict[str, Any],
) -> dict[str, Any]:
    """Keep versions stable for untouched channels; bump changed/new ones.

    Dropped channels are removed, preserving ``versions_seen`` validity for
    channels the migration did not touch.
    """
    reconciled: dict[str, Any] = {}
    for channel, value in new_values.items():
        # Mirror ``coerce_field``'s notion of "changed" recursively: a value that
        # is ``==`` but of a different type at any depth (``1`` -> ``1.0``) is a
        # real change. Plain ``==`` would keep the old version, so the new blob
        # would never be written back and the migration would be silently lost
        # while the checkpoint is still stamped as migrated.
        unchanged = channel in old_values and strict_equal(old_values[channel], value)
        if unchanged and channel in old_versions:
            reconciled[channel] = old_versions[channel]
        else:
            reconciled[channel] = saver.get_next_version(old_versions.get(channel), None)
    return reconciled


def changed_versions(old_checkpoint: Checkpoint, new_checkpoint: Checkpoint) -> ChannelVersions:
    """Channels whose version changed — the blobs to (re)write on write-back."""
    old = old_checkpoint.get("channel_versions", {})
    new = new_checkpoint.get("channel_versions", {})
    return {ch: v for ch, v in new.items() if old.get(ch) != v}


def build_migrated_tuple(
    tup: CheckpointTuple, migrated: StateEnvelope, saver: BaseCheckpointSaver
) -> CheckpointTuple:
    """Return a new tuple carrying the migrated values, versions and revision tag.

    ``pending_writes`` are passed through untouched (single-channel fragments —
    see the limitation note in :mod:`langmigrate.runtime.interceptor`).
    """
    checkpoint = tup.checkpoint
    new_checkpoint: Checkpoint = {
        **checkpoint,
        "channel_values": migrated.values,
        "channel_versions": reconcile_versions(
            saver,
            checkpoint.get("channel_versions", {}),
            checkpoint["channel_values"],
            migrated.values,
        ),
    }
    return CheckpointTuple(
        config=tup.config,
        checkpoint=new_checkpoint,
        metadata=metadata_for(migrated),  # type: ignore[arg-type]
        parent_config=tup.parent_config,
        pending_writes=tup.pending_writes,
    )


def put_config(tup: CheckpointTuple) -> RunnableConfig:
    """Config to re-``put`` under, preserving the parent pointer & checkpoint id.

    ``put`` derives the stored parent from ``config["checkpoint_id"]``, so we write
    back under the *parent* config (or a config with no checkpoint_id for a root
    checkpoint). The checkpoint keeps its own ``id`` from the payload.
    """
    if tup.parent_config is not None:
        return tup.parent_config
    cfg = tup.config["configurable"]
    return {
        "configurable": {
            "thread_id": cfg["thread_id"],
            "checkpoint_ns": cfg.get("checkpoint_ns", ""),
        }
    }
