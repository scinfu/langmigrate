"""LangChain/LangGraph middleware that migrates state on each step.

Drop-in for the managed-platform case (e.g. LangGraph Server) where you don't own
the checkpointer: add :class:`SchemaMigrationMiddleware` to your agent's middleware
stack and old threads are upgraded to the current schema at the earliest middleware
hook reached (``before_agent`` on a fresh pass, ``before_model`` on a mid-loop
resume). See the class docstring for the resume-into-a-tool-node limitation.

Requires ``langchain`` (the ``AgentMiddleware`` base). It is imported lazily so the
rest of LangMigrate stays dependency-light.

Example::

    from langmigrate.integrations.langchain import SchemaMigrationMiddleware

    middleware = SchemaMigrationMiddleware("migrations")  # path or MigrationEngine
    agent = create_agent(model, middleware=[middleware, ...])
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typing_extensions import NotRequired, TypedDict

from ..core.engine import HEAD, MigrationEngine
from ..core.registry import MigrationRegistry
from ..core.types import OnUnknownRevision
from .state import DEFAULT_STATE_REV_KEY, OnRemoved, migrate_state_update


def _resolve_engine(engine_or_path: MigrationEngine | str | Path) -> MigrationEngine:
    if isinstance(engine_or_path, MigrationEngine):
        return engine_or_path
    return MigrationEngine(MigrationRegistry.from_path(engine_or_path))


def _load_agent_middleware() -> Any:
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError as exc:  # pragma: no cover - exercised only without langchain
        raise ImportError(
            "SchemaMigrationMiddleware requires `langchain` (AgentMiddleware). "
            "Install it, or use langmigrate.integrations.state.migrate_state_update "
            "directly in your own node."
        ) from exc
    return AgentMiddleware


# Reserved channel the middleware contributes to the graph state so the revision
# tag is a declared channel (LangGraph rejects updates to undeclared channels).
class _RevisionState(TypedDict):
    langmigrate_rev: NotRequired[str]


def __getattr__(name: str) -> Any:
    # Lazily build the class so importing this module never hard-requires langchain
    # until SchemaMigrationMiddleware is actually accessed.
    if name != "SchemaMigrationMiddleware":
        raise AttributeError(name)

    AgentMiddleware = _load_agent_middleware()

    class SchemaMigrationMiddleware(AgentMiddleware):  # type: ignore[misc, valid-type]
        """Upgrade thread state to the head revision as early as possible.

        Hooks **both** ``before_agent`` (once at the start of a fresh pass) and
        ``before_model`` (every model call, so mid-loop resumes are covered too).
        Both are idempotent: once the state carries the head revision they return
        ``None``.

        Limitation: middleware hooks are graph nodes, so a thread that resumes
        *directly* into a tool node — before any hook runs — sees pre-migration
        state until the next hook. For a hard "before every node" guarantee, own the
        checkpointer and use ``MigrationInterceptor`` (Path A) instead.
        """

        state_schema = _RevisionState

        def __init__(
            self,
            engine_or_path: MigrationEngine | str | Path,
            *,
            target: str = HEAD,
            rev_key: str = DEFAULT_STATE_REV_KEY,
            on_removed: OnRemoved = "warn",
            on_unknown_revision: OnUnknownRevision = "raise",
        ) -> None:
            super().__init__()
            self.engine = _resolve_engine(engine_or_path)
            self.target = target
            self.rev_key = rev_key
            self.on_removed = on_removed
            self.on_unknown_revision = on_unknown_revision
            if rev_key != DEFAULT_STATE_REV_KEY:
                # The contributed state channel must match the configured key:
                # LangGraph rejects updates to undeclared channels, so the fixed
                # class-level schema would break any custom rev_key.
                self.state_schema = TypedDict(  # type: ignore[misc]
                    "_RevisionState", {rev_key: NotRequired[str]}
                )

        def _migrate(self, state: dict[str, Any]) -> dict[str, Any] | None:
            return migrate_state_update(
                self.engine,
                state,
                target=self.target,
                rev_key=self.rev_key,
                on_removed=self.on_removed,
                on_unknown_revision=self.on_unknown_revision,
            )

        def before_agent(self, state: dict[str, Any], runtime: Any = None) -> dict[str, Any] | None:
            return self._migrate(state)

        def before_model(self, state: dict[str, Any], runtime: Any = None) -> dict[str, Any] | None:
            return self._migrate(state)

        async def abefore_agent(
            self, state: dict[str, Any], runtime: Any = None
        ) -> dict[str, Any] | None:
            return self._migrate(state)

        async def abefore_model(
            self, state: dict[str, Any], runtime: Any = None
        ) -> dict[str, Any] | None:
            return self._migrate(state)

    # Cache the class in the module namespace: PEP 562 __getattr__ runs on every
    # access, and rebuilding would hand out a *different* class object each time,
    # breaking isinstance checks across imports.
    globals()["SchemaMigrationMiddleware"] = SchemaMigrationMiddleware
    return SchemaMigrationMiddleware
