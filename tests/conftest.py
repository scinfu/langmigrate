"""Shared pytest fixtures for LangMigrate tests."""

from __future__ import annotations

from typing import Any

import pytest

from langmigrate.core.types import StateEnvelope


@pytest.fixture
def envelope() -> StateEnvelope:
    """A small state envelope at no particular revision."""
    return StateEnvelope(values={"messages": ["hi"], "count": 1}, revision=None)


def make_envelope(values: dict[str, Any], revision: str | None = None) -> StateEnvelope:
    """Helper to build envelopes inline inside tests."""
    return StateEnvelope(values=values, revision=revision)
