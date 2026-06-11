"""Agent graph: ``create_agent`` healed by ``SchemaMigrationMiddleware``.

Pattern: on a managed platform (LangGraph Studio / Server) you don't own the
checkpointer, but agents built with ``create_agent`` have a middleware stack.
``SchemaMigrationMiddleware`` hooks ``before_agent`` / ``before_model`` and
upgrades the thread state to the head revision before any v2 code reads it.

The v2 schema adds a ``user_profile`` channel (contributed by
``ProfileMiddleware.state_schema``) that ``before_model`` *requires* — old v1
threads (and, without migration, even new threads) are missing it and fail.

Demo toggles — edit, save, and ``langgraph dev`` hot-reloads (threads survive):

1. ``SCHEMA_VERSION = 1`` / ``LANGMIGRATE_ENABLED = False`` — chat, create threads.
2. ``SCHEMA_VERSION = 2`` — any turn now raises ``SchemaOutOfDateError``.
3. ``LANGMIGRATE_ENABLED = True`` — the middleware injects the default profile
   (revision a1c0) and every thread, old or new, works again.

Model: uses Claude (``anthropic:claude-opus-4-8``) when ``ANTHROPIC_API_KEY`` is
set, otherwise a deterministic offline echo model — the migration demo is
identical either way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool
from typing_extensions import NotRequired, TypedDict

from langmigrate.integrations.langchain import SchemaMigrationMiddleware

from ..common import SchemaOutOfDateError
from .fake_model import EchoToolModel

# ---------------------------------------------------------------------------
# DEMO TOGGLES — edit these two lines during the walkthrough.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1  # 1 = plain agent, 2 = requires the `user_profile` channel
LANGMIGRATE_ENABLED = False  # True = SchemaMigrationMiddleware heals stale threads

MIGRATIONS = Path(__file__).parent / "migrations"


class _ProfileState(TypedDict):
    user_profile: NotRequired[dict[str, Any]]


class ProfileMiddleware(AgentMiddleware):
    """v2 feature: personalisation that strictly requires ``user_profile``."""

    state_schema = _ProfileState

    def _check(self, state: dict[str, Any]) -> None:
        if "user_profile" not in state:
            raise SchemaOutOfDateError(
                "Schema v2 requires the 'user_profile' channel, but this thread "
                "does not have it (it was persisted with schema v1, or migration "
                "is disabled). Fix: set LANGMIGRATE_ENABLED = True in "
                "studio_graphs/agent/graph.py so SchemaMigrationMiddleware "
                "injects the default profile (revision a1c0), then resume."
            )

    def before_model(self, state: dict[str, Any], runtime: Any = None) -> None:
        self._check(state)

    async def abefore_model(self, state: dict[str, Any], runtime: Any = None) -> None:
        self._check(state)


@tool
def add_numbers(a: float, b: float) -> float:
    """Add two numbers and return the result."""
    return a + b


def _make_model() -> Any:
    if os.environ.get("ANTHROPIC_API_KEY"):
        from langchain.chat_models import init_chat_model

        return init_chat_model(os.environ.get("STUDIO_AGENT_MODEL", "anthropic:claude-opus-4-8"))
    return EchoToolModel()


def _make_middleware() -> list[AgentMiddleware]:
    middleware: list[AgentMiddleware] = []
    if LANGMIGRATE_ENABLED:
        # Must run first so the state is healed before ProfileMiddleware reads it.
        middleware.append(SchemaMigrationMiddleware(MIGRATIONS))
    if SCHEMA_VERSION >= 2:
        middleware.append(ProfileMiddleware())
    return middleware


graph = create_agent(
    _make_model(),
    tools=[add_numbers],
    system_prompt=(
        "You are a concise demo assistant for LangMigrate. "
        "Use the add_numbers tool for any arithmetic the user asks for."
    ),
    middleware=_make_middleware(),
)
