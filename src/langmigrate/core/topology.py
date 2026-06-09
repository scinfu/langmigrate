"""Graph topology evolution: repairing interrupted threads.

When a thread is interrupted (paused) on a node that a later graph version renamed
or deleted, resuming it would deadlock or raise. A :class:`NodeRemap` declares how
old node names map onto the current graph so a stuck thread can be redirected to a
valid node (or blocked with a structured :class:`TopologyMismatchError`).

This is pure logic: it operates on a :class:`StateEnvelope`'s ``node`` field and an
optional set of currently-known node names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .exceptions import TopologyMismatchError
from .types import StateEnvelope


class NodeRemap:
    """Declarative mapping from old node names to the current graph topology.

    - ``renames`` redirect a removed node to its replacement.
    - ``removed`` lists nodes deleted with no direct replacement; such threads are
      redirected to ``fallback`` if given, otherwise blocked.
    - With ``known_nodes`` passed to :meth:`resolve`/:meth:`apply`, any node not in
      that set (and not otherwise handled) is treated as removed.
    """

    def __init__(
        self,
        renames: Mapping[str, str] | None = None,
        removed: Iterable[str] | None = None,
        *,
        fallback: str | None = None,
    ) -> None:
        self.renames: dict[str, str] = dict(renames or {})
        self.removed: set[str] = set(removed or ())
        self.fallback = fallback

    def resolve(self, node: str, *, known_nodes: Iterable[str] | None = None) -> str:
        """Return the node a stuck thread should resume on, or raise if unmappable."""
        if node in self.renames:
            return self.renames[node]
        known = set(known_nodes) if known_nodes is not None else None
        is_missing = node in self.removed or (known is not None and node not in known)
        if is_missing:
            if self.fallback is not None:
                return self.fallback
            raise TopologyMismatchError(node, known_nodes=sorted(known) if known else None)
        return node

    def apply(
        self, state: StateEnvelope, *, known_nodes: Iterable[str] | None = None
    ) -> StateEnvelope:
        """Return ``state`` with its ``node`` remapped to the current topology."""
        if state.node is None:
            return state
        new_node = self.resolve(state.node, known_nodes=known_nodes)
        if new_node == state.node:
            return state
        return state.model_copy(update={"node": new_node})
