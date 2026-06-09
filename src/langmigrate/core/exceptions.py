"""Structured exception hierarchy for LangMigrate.

All errors derive from :class:`LangMigrateError` so callers can catch the whole
family. Errors carry structured attributes (not just a message) so the CLI and
runtime can render actionable diagnostics.
"""

from __future__ import annotations


class LangMigrateError(Exception):
    """Base class for every error raised by LangMigrate."""


class UnsafeMigrationError(LangMigrateError):
    """An unsafe operation was attempted without the required handling.

    Raised, for example, when a field rename or type change is requested but the
    source data cannot be transformed safely.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class MissingRequiredFieldError(UnsafeMigrationError):
    """A required field has no value and no default/fallback was provided."""

    def __init__(self, field: str, *, revision: str | None = None) -> None:
        super().__init__(
            f"Required field {field!r} is missing and no fallback was provided"
            + (f" (revision {revision})" if revision else ""),
            field=field,
        )
        self.revision = revision


class RevisionNotFoundError(LangMigrateError):
    """A revision id was referenced but not found in the registry."""

    def __init__(self, revision: str) -> None:
        super().__init__(f"Revision {revision!r} not found in the migration registry")
        self.revision = revision


class DuplicateRevisionError(LangMigrateError):
    """Two migrations declare the same ``revision`` id."""

    def __init__(self, revision: str) -> None:
        super().__init__(f"Duplicate revision id {revision!r} found in the migration set")
        self.revision = revision


class MultipleHeadsError(LangMigrateError):
    """The revision DAG has no single head.

    A *head* is a revision that no other revision points to via ``down_revision``.
    Raised both when the history branches into more than one head (create a merge
    revision or target a specific head) and when there are no revisions at all.
    """

    def __init__(self, heads: list[str]) -> None:
        if heads:
            message = (
                "The migration history has multiple heads: "
                + ", ".join(sorted(heads))
                + ". Create a merge revision or target a specific head."
            )
        else:
            message = "The migration history has no revisions yet (run `langmigrate revision`)."
        super().__init__(message)
        self.heads = list(heads)


class CyclicHistoryError(LangMigrateError):
    """The revision graph contains a cycle and cannot be linearized."""

    def __init__(self, revisions: list[str]) -> None:
        super().__init__("Cycle detected in migration history involving: " + ", ".join(revisions))
        self.revisions = list(revisions)


class IrreversibleMigrationError(LangMigrateError):
    """A migration declared itself irreversible and ``downgrade`` was attempted."""

    def __init__(self, revision: str) -> None:
        super().__init__(f"Migration {revision!r} is irreversible and cannot be downgraded")
        self.revision = revision


class TopologyMismatchError(LangMigrateError):
    """An interrupted thread points to a node that no longer exists in the graph."""

    def __init__(self, node: str, *, known_nodes: list[str] | None = None) -> None:
        super().__init__(
            f"Interrupted thread references node {node!r} which is not in the current graph"
            + (f" (known nodes: {', '.join(known_nodes)})" if known_nodes else "")
        )
        self.node = node
        self.known_nodes = list(known_nodes) if known_nodes else []


class RevisionNotAncestorError(LangMigrateError):
    """A path was requested between two known revisions that aren't on the same line.

    e.g. downgrading to a revision that sits *above* the current one, or upgrading
    from a revision that isn't an ancestor of the target. The revision exists — it
    just isn't reachable in the requested direction.
    """

    def __init__(self, revision: str, other: str, *, direction: str) -> None:
        if direction == "downgrade":
            msg = (
                f"Cannot downgrade to {revision!r}: it is not an ancestor of the current "
                f"revision {other!r} (it sits above it)."
            )
        else:
            msg = (
                f"Cannot upgrade from {revision!r} to {other!r}: {revision!r} is not an "
                f"ancestor of {other!r}."
            )
        super().__init__(msg)
        self.revision = revision
        self.other = other
        self.direction = direction


class ChannelRemovalUnsupportedError(LangMigrateError):
    """A state-level migration tried to remove channels, which LangGraph can't merge.

    State updates are merged, so a rename/drop cannot delete the old channel at the
    state level. Own the checkpointer and use ``MigrationInterceptor`` to purge them.
    """

    def __init__(self, channels: list[str]) -> None:
        super().__init__(
            "State-level migration cannot remove channel(s) "
            + ", ".join(sorted(channels))
            + " — LangGraph merges state updates, so the old key(s) would linger. "
            "Use MigrationInterceptor (the saver path) to purge them."
        )
        self.channels = list(channels)
