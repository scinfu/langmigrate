"""Lazy online migration for LangGraph stores: a ``BaseStore`` wrapper.

:class:`MigrationStore` wraps any ``BaseStore`` and migrates item values lazily.
Stores have no metadata channel, so the revision tag lives under the reserved key
``langmigrate_rev`` *inside the stored value*: it is injected on every put and
stripped from every item returned, so application code never sees it.

All ``BaseStore`` convenience methods (``get``/``put``/``search``/``delete`` and
their async twins) route through ``batch``/``abatch``, so intercepting those two
methods covers the entire surface.

- ``get``/``aget`` migrate lazily and (by default) write the healed value back.
- ``search``/``asearch`` migrate **in memory only, never writing back** — they
  enumerate many items and healing there would be a write storm. The proactive
  path is the batch runner (``langmigrate store upgrade``).

Write-back re-puts through the wrapped store's default indexing configuration and
bumps ``updated_at`` — acceptable for healing; use the batch runner to cure the
whole store deliberately.
"""

from __future__ import annotations

from collections.abc import Iterable

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from ..core.engine import HEAD, MigrationEngine
from ..core.exceptions import RevisionNotFoundError
from ..core.types import StateEnvelope
from ..core.version import envelope_from_item_parts, stamp_value, strip_value_tag, value_for
from .interceptor import OnUnknownRevision, logger


class MigrationStore(BaseStore):
    """Wrap a store to migrate item values lazily on read and tag them on write."""

    def __init__(
        self,
        store: BaseStore,
        engine: MigrationEngine,
        *,
        write_back: bool = True,
        target: str = HEAD,
        on_unknown_revision: OnUnknownRevision = "raise",
    ) -> None:
        self.store = store
        self.engine = engine
        self.write_back = write_back
        self.target = target
        self.on_unknown_revision = on_unknown_revision
        # Mirror the wrapped store's TTL surface so ttl= arguments validate alike.
        self.supports_ttl = store.supports_ttl
        self.ttl_config = store.ttl_config

    # -- the whole BaseStore API routes through batch/abatch -----------------

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        prepared = self._prepare(list(ops))
        results = self.store.batch(prepared)
        return [self._postprocess(op, result) for op, result in zip(prepared, results, strict=True)]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        prepared = self._prepare(list(ops))
        results = await self.store.abatch(prepared)
        return [
            await self._apostprocess(op, result)
            for op, result in zip(prepared, results, strict=True)
        ]

    # -- internals ------------------------------------------------------------

    def _prepare(self, ops: list[Op]) -> list[Op]:
        """Stamp outgoing PutOp values with the resolved target revision."""
        prepared: list[Op] = []
        for op in ops:
            if isinstance(op, PutOp) and op.value is not None:
                target_rev = self.engine.resolve_target(self.target)
                prepared.append(op._replace(value=stamp_value(dict(op.value), target_rev)))
            else:
                prepared.append(op)
        return prepared

    def _postprocess(self, op: Op, result: Result) -> Result:
        if isinstance(op, GetOp) and isinstance(result, Item):
            migrated, changed = self._migrate_item(result)
            if changed and self.write_back:
                self.store.put(result.namespace, result.key, value_for(migrated))
            return rebuild_item(result, migrated)
        if isinstance(op, SearchOp) and isinstance(result, list):
            # In-memory migration only: search enumerates many items and healing
            # here would be a write storm. Use `langmigrate store upgrade`.
            return [self._migrated_view(item) for item in result]
        return result

    async def _apostprocess(self, op: Op, result: Result) -> Result:
        if isinstance(op, GetOp) and isinstance(result, Item):
            migrated, changed = self._migrate_item(result)
            if changed and self.write_back:
                await self.store.aput(result.namespace, result.key, value_for(migrated))
            return rebuild_item(result, migrated)
        if isinstance(op, SearchOp) and isinstance(result, list):
            return [self._migrated_view(item) for item in result]
        return result

    def _migrate_item(self, item: Item) -> tuple[StateEnvelope, bool]:
        """Return (migrated envelope, whether the cascade changed anything)."""
        envelope = envelope_from_item_parts(item.value, namespace=item.namespace, key=item.key)
        try:
            migrated = self.engine.upgrade_state(envelope, self.target)
        except RevisionNotFoundError as exc:
            if self.on_unknown_revision == "raise" or exc.revision != envelope.revision:
                raise
            if self.on_unknown_revision == "warn":
                logger.warning(
                    "langmigrate: store item %s/%s carries unknown revision %r (not in "
                    "the registry); returning it unmigrated. This usually means the "
                    "code was rolled back after a lazy migration.",
                    "/".join(item.namespace),
                    item.key,
                    exc.revision,
                )
            return envelope, False
        return migrated, migrated is not envelope

    def _migrated_view(self, item: Item) -> Item:
        migrated, _ = self._migrate_item(item)
        return rebuild_item(item, migrated)


def rebuild_item(item: Item, envelope: StateEnvelope) -> Item:
    """Item carrying the envelope's (tag-free) values; preserves identity fields."""
    values = strip_value_tag(envelope.values)
    if isinstance(item, SearchItem):
        return SearchItem(
            namespace=item.namespace,
            key=item.key,
            value=values,
            created_at=item.created_at,
            updated_at=item.updated_at,
            score=item.score,
        )
    return Item(
        value=values,
        key=item.key,
        namespace=item.namespace,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
