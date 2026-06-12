"""The migration registry: discovery and DAG resolution.

The registry holds the set of :class:`BaseMigration` instances, validates the
revision graph (duplicates, cycles, unknown parents), and resolves the ordered
path of revisions to apply for an upgrade or downgrade.

Revisions form an Alembic-style DAG via ``down_revision`` pointers; a **merge
revision** declares a tuple of parents to join branched heads. Paths are resolved
as ancestor-set differences, linearized deterministically (Kahn's algorithm with
ties broken on the smallest revision id), then applied by the engine as a linear
cascade.
"""

from __future__ import annotations

import heapq
import importlib.util
import sys
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

from .exceptions import (
    CyclicHistoryError,
    DuplicateRevisionError,
    InvalidMigrationGraphError,
    MultipleHeadsError,
    RevisionNotAncestorError,
    RevisionNotFoundError,
)
from .migration import BaseMigration


class MigrationRegistry:
    """An ordered, validated collection of migrations forming a revision DAG."""

    def __init__(self, migrations: Iterable[BaseMigration]) -> None:
        self._by_rev: dict[str, BaseMigration] = {}
        for m in migrations:
            if not isinstance(m.revision, str):
                raise TypeError(
                    f"Migration {m!r} has a non-string revision id: "
                    f"{m.revision!r} (type {type(m.revision).__name__})"
                )
            if not m.revision:
                raise ValueError(f"Migration {m!r} has an empty revision id")
            if m.revision in self._by_rev:
                raise DuplicateRevisionError(m.revision)
            self._by_rev[m.revision] = m
        # The registry is immutable after construction, so derived DAG state is
        # built once and memoized: parent -> children edges, per-revision
        # ancestor sets (filled lazily), and the head list. Built *before*
        # _validate so the validator can use ``ancestors()`` for the
        # redundant-merge-parents check. Unknown parents are skipped here —
        # they are rejected by _validate with the proper RevisionNotFoundError
        # rather than crashing on a KeyError.
        self._children: dict[str, list[str]] = {rev: [] for rev in self._by_rev}
        for m in self._by_rev.values():
            for parent in m.parents:
                if parent in self._children:
                    self._children[parent].append(m.revision)
        self._ancestors: dict[str, frozenset[str]] = {}
        self._validate()
        self._heads: list[str] = [rev for rev in self._by_rev if not self._children[rev]]

    # -- construction -------------------------------------------------------

    @classmethod
    def from_migrations(cls, migrations: Iterable[BaseMigration]) -> MigrationRegistry:
        """Build a registry from already-instantiated migrations."""
        return cls(migrations)

    @classmethod
    def from_path(cls, path: str | Path) -> MigrationRegistry:
        """Discover migrations by importing every ``*.py`` file under ``path``.

        Each module may define one or more :class:`BaseMigration` subclasses; each
        such subclass is instantiated once and registered.

        Files may import one another (``from v1 import M1``) in any direction:
        such imports resolve to the same objects discovered here, regardless of
        the order in which files are processed, and without the caller having to
        put ``path`` on ``sys.path``.
        """
        directory = Path(path)
        if not directory.is_dir():
            raise FileNotFoundError(f"Migrations directory not found: {directory}")
        files = [f for f in sorted(directory.glob("*.py")) if not f.name.startswith("_")]
        migrations: list[BaseMigration] = []
        seen: set[int] = set()
        # Migration files may import one another (e.g. ``from v1 import M1``). For
        # those imports to resolve to the *same* class objects we discover here —
        # rather than duplicate objects that trip DuplicateRevisionError — each
        # module must be visible under its bare stem in ``sys.modules`` while the
        # others load, and the directory must be importable so that a *forward*
        # import (of a file not yet processed) resolves too. We expose both only
        # for the duration of discovery and restore the prior state afterwards, so
        # we never permanently shadow a real module, pollute ``sys.path``, nor
        # serve a stale module object on a later call.
        dir_str = str(directory)
        added_to_path = dir_str not in sys.path
        stems = {f.stem for f in files}
        saved = {name: sys.modules[name] for name in stems if name in sys.modules}
        if added_to_path:
            sys.path.insert(0, dir_str)
        try:
            for file in files:
                migrations.extend(_load_migrations_from_file(file, seen))
        finally:
            if added_to_path and dir_str in sys.path:
                sys.path.remove(dir_str)
            for name in stems:
                if name in saved:
                    sys.modules[name] = saved[name]
                else:
                    sys.modules.pop(name, None)
        return cls(migrations)

    # -- lookups ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_rev)

    def __iter__(self) -> Iterator[BaseMigration]:
        return iter(self._by_rev.values())

    def __contains__(self, revision: str) -> bool:
        return revision in self._by_rev

    def get(self, revision: str) -> BaseMigration:
        """Return the migration with ``revision`` or raise."""
        try:
            return self._by_rev[revision]
        except KeyError:
            raise RevisionNotFoundError(revision) from None

    def bases(self) -> list[str]:
        """Revisions with no parent (``down_revision is None``)."""
        return [m.revision for m in self._by_rev.values() if m.down_revision is None]

    def heads(self) -> list[str]:
        """Revisions that are nobody's parent (the tips of the DAG)."""
        return list(self._heads)

    def head(self) -> str:
        """The single head, or raise :class:`MultipleHeadsError`."""
        heads = self.heads()
        if not heads:
            raise MultipleHeadsError([])
        if len(heads) > 1:
            raise MultipleHeadsError(heads)
        return heads[0]

    # -- path resolution ----------------------------------------------------

    def ancestors(self, revision: str) -> frozenset[str]:
        """Strict ancestor set of ``revision`` (excludes ``revision`` itself)."""
        cached = self._ancestors.get(revision)
        if cached is not None:
            return cached
        result: set[str] = set()
        stack = list(self.get(revision).parents)
        while stack:
            rev = stack.pop()
            if rev in result:
                continue
            if rev not in self._by_rev:
                raise RevisionNotFoundError(rev)
            result.add(rev)
            cached = self._ancestors.get(rev)
            if cached is not None:
                result.update(cached)
            else:
                stack.extend(self._by_rev[rev].parents)
        frozen = frozenset(result)
        self._ancestors[revision] = frozen
        return frozen

    def lineage(self, revision: str) -> list[str]:
        """All ancestors of ``revision`` plus itself, in topological order.

        Deterministic (ties broken on the smallest revision id) and ending at
        ``revision``. For linear histories this is exactly the old
        ``[base, ..., revision]`` chain.
        """
        return self._topo_sort(set(self.ancestors(revision)) | {revision})

    def upgrade_path(self, from_revision: str | None, to_revision: str) -> list[str]:
        """Revisions to apply (in order) to go from ``from_revision`` up to ``to``.

        ``from_revision`` of ``None`` means "untagged / base" — apply the whole
        lineage. Raises if ``from_revision`` is not an ancestor of ``to``.

        With merges, the path is the topological linearization of
        ``ancestors(to) - ancestors(from)`` (both inclusive). Since
        ``from ∈ ancestors(to)`` implies ``ancestors(from) ⊆ ancestors(to)``,
        the difference is well-defined; every parent of a diff revision is either
        in the diff or already applied, so the restricted in-degrees are exact and
        ``to`` always comes last.
        """
        target_set = set(self.ancestors(to_revision)) | {to_revision}
        if from_revision is None:
            return self._topo_sort(target_set)
        if from_revision == to_revision:
            return []
        if from_revision not in self._by_rev:
            raise RevisionNotFoundError(from_revision)
        if from_revision not in target_set:
            raise RevisionNotAncestorError(from_revision, to_revision, direction="upgrade")
        done = set(self.ancestors(from_revision)) | {from_revision}
        return self._topo_sort(target_set - done)

    def downgrade_path(self, from_revision: str, to_revision: str | None) -> list[str]:
        """Revisions to reverse (in order) to go from ``from`` down to ``to``.

        ``to_revision`` of ``None`` downgrades all the way past the base. Each listed
        revision should have its ``downgrade`` applied, newest first (the reverse of
        the corresponding upgrade path).
        """
        src_set = set(self.ancestors(from_revision)) | {from_revision}
        if to_revision is None:
            return list(reversed(self._topo_sort(src_set)))
        if to_revision == from_revision:
            return []
        if to_revision not in self._by_rev:
            raise RevisionNotFoundError(to_revision)
        if to_revision not in src_set:
            raise RevisionNotAncestorError(to_revision, from_revision, direction="downgrade")
        keep = set(self.ancestors(to_revision)) | {to_revision}
        return list(reversed(self._topo_sort(src_set - keep)))

    def _topo_sort(self, revs: set[str]) -> list[str]:
        """Kahn's algorithm restricted to ``revs``, deterministic via a min-heap.

        An edge parent->child counts only when both ends are in ``revs`` (parents
        outside are, by construction of the callers, already applied). Ties are
        broken on the smallest revision id so the linearization is stable across
        processes and Python versions.
        """
        indegree = {rev: sum(1 for p in self._by_rev[rev].parents if p in revs) for rev in revs}
        heap = [rev for rev, deg in indegree.items() if deg == 0]
        heapq.heapify(heap)
        out: list[str] = []
        while heap:
            rev = heapq.heappop(heap)
            out.append(rev)
            for child in self._children.get(rev, ()):
                if child in revs:
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        heapq.heappush(heap, child)
        if len(out) != len(revs):  # pragma: no cover - cycles are caught in _validate
            raise CyclicHistoryError(sorted(set(revs) - set(out)))
        return out

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        for m in self._by_rev.values():
            parents = m.parents
            if len(parents) != len(set(parents)):
                raise ValueError(
                    f"Migration {m.revision!r} lists a duplicate parent in down_revision"
                )
            for parent in parents:
                if parent not in self._by_rev:
                    raise RevisionNotFoundError(parent)
            # A merge's parents must be independent branches: if one parent is
            # an ancestor of another, the edge is redundant (its effect is
            # already covered by the descendant). The CLI's `langmigrate
            # merge` enforces this invariant; the registry does too so
            # hand-written merges (or merges that became redundant after a
            # later revision was added) cannot bypass the check. Note: the
            # resulting cascade is *the same* with or without the redundant
            # edge — the topological sort and ancestor-set difference in
            # ``upgrade_path`` / ``downgrade_path`` ignore the redundancy. This
            # is a hygiene / clarity check, not a correctness fix.
            if len(parents) > 1:
                ancestor_sets = {p: self.ancestors(p) for p in parents}
                for p1, p2 in ((a, b) for a in parents for b in parents if a != b):
                    if p1 in ancestor_sets[p2]:
                        raise InvalidMigrationGraphError(
                            f"Migration {m.revision!r} merges a parent "
                            f"({p1!r}) with its own descendant ({p2!r}): the "
                            f"edge is redundant. Drop {p1!r} from "
                            f"down_revision (the cascade is identical without it)."
                        )
        self._check_acyclic()

    def _check_acyclic(self) -> None:
        """Three-color iterative DFS over parent edges; raises on any cycle."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self._by_rev, WHITE)
        for start in self._by_rev:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, bool]] = [(start, False)]
            path: list[str] = []
            while stack:
                rev, processed = stack.pop()
                if processed:
                    color[rev] = BLACK
                    path.pop()
                    continue
                if color[rev] == BLACK:
                    continue
                if color[rev] == GRAY:
                    raise CyclicHistoryError(path[path.index(rev) :] + [rev])
                color[rev] = GRAY
                path.append(rev)
                stack.append((rev, True))
                for parent in self._by_rev[rev].parents:
                    if parent == rev:
                        # A self-loop (``down_revision`` pointing to ``self``)
                        # would otherwise emit a duplicated node in the path
                        # (``[rev, rev]``); surface it as a clear single-node
                        # self-loop instead so log parsers and humans can
                        # recognise the pattern.
                        raise CyclicHistoryError([f"{rev} (self-loop)"])
                    if color[parent] == GRAY:
                        raise CyclicHistoryError(path[path.index(parent) :] + [parent])
                    if color[parent] == WHITE:
                        stack.append((parent, False))


def new_revision_id() -> str:
    """Generate a fresh Alembic-style revision id (12-char hex)."""
    return uuid.uuid4().hex[:12]


def _load_migrations_from_file(file: Path, seen: set[int]) -> list[BaseMigration]:
    # Load the file under its bare stem so that sibling files importing it (e.g.
    # ``from v1 import ...``) resolve to this very module object — and thus the
    # same class IDs, which the shared ``seen`` set dedupes to avoid
    # DuplicateRevisionError. Reuse an already-loaded module only when it is
    # *this* file (matched by path): a same-named entry left by a real package
    # must not be mistaken for our migration module.
    module_name = file.stem
    existing = sys.modules.get(module_name)
    existing_file = getattr(existing, "__file__", None)
    if existing is not None and existing_file is not None and Path(existing_file) == file:
        module = existing
    else:
        spec = importlib.util.spec_from_file_location(module_name, file)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            return []
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

    found: list[BaseMigration] = []
    for obj in vars(module).values():
        if id(obj) in seen:
            continue
        # A subclass of BaseMigration (the `class Migration(BaseMigration)` style).
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseMigration)
            and obj is not BaseMigration
            and getattr(obj, "revision", "")  # type: ignore[arg-type]
        ):
            seen.add(id(obj))
            found.append(obj())
        # An already-built instance (the `@migration(...)` decorator style).
        elif isinstance(obj, BaseMigration) and obj.revision:
            seen.add(id(obj))
            found.append(obj)
    return found
