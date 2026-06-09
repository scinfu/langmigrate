"""The migration registry: discovery and DAG resolution.

The registry holds the set of :class:`BaseMigration` instances, validates the
revision graph (duplicates, cycles, unknown parents), and resolves the ordered
path of revisions to apply for an upgrade or downgrade.

Revisions form an Alembic-style DAG via ``down_revision`` pointers. The engine
resolves a path here, then applies it as a linear cascade.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

from .exceptions import (
    CyclicHistoryError,
    DuplicateRevisionError,
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
            if not m.revision:
                raise ValueError(f"Migration {m!r} has an empty revision id")
            if m.revision in self._by_rev:
                raise DuplicateRevisionError(m.revision)
            self._by_rev[m.revision] = m
        self._validate()

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
        """Revisions that are nobody's ``down_revision`` (the tips of the DAG)."""
        referenced = {m.down_revision for m in self._by_rev.values() if m.down_revision is not None}
        return [rev for rev in self._by_rev if rev not in referenced]

    def head(self) -> str:
        """The single head, or raise :class:`MultipleHeadsError`."""
        heads = self.heads()
        if not heads:
            raise MultipleHeadsError([])
        if len(heads) > 1:
            raise MultipleHeadsError(heads)
        return heads[0]

    # -- path resolution ----------------------------------------------------

    def lineage(self, revision: str) -> list[str]:
        """Ordered chain ``[base, ..., revision]`` following ``down_revision``."""
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = revision
        while cur is not None:
            if cur in seen:
                raise CyclicHistoryError(chain + [cur])
            if cur not in self._by_rev:
                raise RevisionNotFoundError(cur)
            seen.add(cur)
            chain.append(cur)
            cur = self._by_rev[cur].down_revision
        chain.reverse()
        return chain

    def upgrade_path(self, from_revision: str | None, to_revision: str) -> list[str]:
        """Revisions to apply (in order) to go from ``from_revision`` up to ``to``.

        ``from_revision`` of ``None`` means "untagged / base" — apply the whole
        lineage. Raises if ``from_revision`` is not an ancestor of ``to``.
        """
        lineage = self.lineage(to_revision)
        if from_revision is None:
            return lineage
        if from_revision == to_revision:
            return []
        if from_revision not in lineage:
            if from_revision in self._by_rev:
                raise RevisionNotAncestorError(from_revision, to_revision, direction="upgrade")
            raise RevisionNotFoundError(from_revision)
        return lineage[lineage.index(from_revision) + 1 :]

    def downgrade_path(self, from_revision: str, to_revision: str | None) -> list[str]:
        """Revisions to reverse (in order) to go from ``from`` down to ``to``.

        ``to_revision`` of ``None`` downgrades all the way past the base. Each listed
        revision should have its ``downgrade`` applied, newest first.
        """
        lineage = self.lineage(from_revision)
        if to_revision is None:
            return list(reversed(lineage))
        if to_revision == from_revision:
            return []
        if to_revision not in lineage:
            if to_revision in self._by_rev:
                raise RevisionNotAncestorError(to_revision, from_revision, direction="downgrade")
            raise RevisionNotFoundError(to_revision)
        return list(reversed(lineage[lineage.index(to_revision) + 1 :]))

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        for m in self._by_rev.values():
            if m.down_revision is not None and m.down_revision not in self._by_rev:
                raise RevisionNotFoundError(m.down_revision)
        # Walking each head's lineage surfaces any cycle via CyclicHistoryError.
        for rev in self._by_rev:
            self.lineage(rev)


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
