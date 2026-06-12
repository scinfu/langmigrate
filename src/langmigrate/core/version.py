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
    """Return a copy of ``metadata`` with the revision tag set to ``revision``.

    :data:`REVISION_METADATA_KEY` is a **reserved** key in checkpoint metadata:
    callers should not store application data under it. The wrapper layer
    (``MigrationInterceptor``) overwrites any pre-existing value on every
    ``put``. Unlike store values, checkpoint metadata is not application state,
    so there is no collision-detection policy on this path.
    """
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


# -- store items --------------------------------------------------------------
#
# Store items have no metadata channel: ``Item.value`` is the only persisted
# payload, so the revision tag lives under the reserved REVISION_METADATA_KEY
# *inside the value*. The runtime wrapper injects it on write and strips it from
# every item it returns, so application code (and migrations) never observe it.

#: Envelope metadata key carrying a store item's namespace tuple.
ITEM_NAMESPACE_META_KEY = "langmigrate_namespace"
#: Envelope metadata key carrying a store item's key.
ITEM_KEY_META_KEY = "langmigrate_key"


def read_value_revision(value: dict[str, Any] | None) -> str | None:
    """Return the revision tag stored inside an item ``value``, or ``None``."""
    if not value:
        return None
    revision = value.get(REVISION_METADATA_KEY)
    return revision if isinstance(revision, str) else None


def stamp_value(value: dict[str, Any], revision: str) -> dict[str, Any]:
    """Return a copy of ``value`` with the revision tag set to ``revision``.

    :data:`REVISION_METADATA_KEY` is a **reserved** key in store item values:
    callers should not store application data under it. The wrapper layer
    (``MigrationStore``) silently overwrites any pre-existing value on every
    ``put`` — see the ``on_reserved_key_collision`` parameter for a warning /
    raise policy that detects the collision.
    """
    new_value = dict(value)
    new_value[REVISION_METADATA_KEY] = revision
    return new_value


def strip_value_tag(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``value`` without the revision tag.

    ``None`` is treated as "no payload" and yields ``{}``. LangGraph's own
    stores never produce an ``Item`` with ``value=None`` (``PutOp(value=None)``
    means *delete*, and ``put`` requires a dict), but external or custom
    ``BaseStore`` implementations can — and the wrapper must serve such items
    back rather than crash on ``dict(None)``. The migration paths skip
    ``None``-valued items entirely, so the original ``None`` is preserved
    end-to-end.
    """
    if value is None:
        return {}
    new_value = dict(value)
    new_value.pop(REVISION_METADATA_KEY, None)
    return new_value


def envelope_from_item_parts(
    value: dict[str, Any], *, namespace: tuple[str, ...], key: str
) -> StateEnvelope:
    """Build a :class:`StateEnvelope` from a store item's parts.

    The revision is read from the in-value tag and the tag is stripped from
    ``values`` so migrations never see it. ``namespace``/``key`` are carried in
    the envelope metadata (under :data:`ITEM_NAMESPACE_META_KEY` /
    :data:`ITEM_KEY_META_KEY`) so migrations can dispatch per namespace.
    """
    return StateEnvelope(
        values=strip_value_tag(value),
        revision=read_value_revision(value),
        metadata={ITEM_NAMESPACE_META_KEY: namespace, ITEM_KEY_META_KEY: key},
    )


def value_for(envelope: StateEnvelope) -> dict[str, Any]:
    """Return the envelope's values as a store ``value``, tagged with its revision."""
    if envelope.revision is None:
        return strip_value_tag(envelope.values)
    return stamp_value(envelope.values, envelope.revision)
