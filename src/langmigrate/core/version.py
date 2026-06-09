"""Reading and writing the schema-version tag.

The tag lives ONLY in a checkpoint's ``metadata`` under
:data:`REVISION_METADATA_KEY` — never inside ``channel_values`` (it is metadata,
not application state, and must stay queryable at the database level).

These helpers work on plain dicts so the core stays free of any LangGraph or
database imports. The runtime/adapters bridge real checkpoints to these dicts.
"""

from __future__ import annotations

from typing import Any

from .types import REVISION_METADATA_KEY, StateEnvelope


def read_revision(metadata: dict[str, Any] | None) -> str | None:
    """Return the revision tag from ``metadata``, or ``None`` if untagged."""
    if not metadata:
        return None
    value = metadata.get(REVISION_METADATA_KEY)
    return value if isinstance(value, str) else None


def stamp_metadata(metadata: dict[str, Any] | None, revision: str) -> dict[str, Any]:
    """Return a copy of ``metadata`` with the revision tag set to ``revision``."""
    new_meta = dict(metadata or {})
    new_meta[REVISION_METADATA_KEY] = revision
    return new_meta


def envelope_from_parts(
    channel_values: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    *,
    node: str | None = None,
) -> StateEnvelope:
    """Build a :class:`StateEnvelope` from a checkpoint's parts.

    The envelope's ``revision`` is read from the metadata tag.
    """
    meta = dict(metadata or {})
    return StateEnvelope(
        values=dict(channel_values),
        revision=read_revision(meta),
        metadata=meta,
        node=node,
    )


def metadata_for(envelope: StateEnvelope) -> dict[str, Any]:
    """Return the envelope's metadata stamped with its current revision.

    If the envelope has no revision the metadata is returned without a tag.
    """
    if envelope.revision is None:
        meta = dict(envelope.metadata)
        meta.pop(REVISION_METADATA_KEY, None)
        return meta
    return stamp_metadata(envelope.metadata, envelope.revision)
