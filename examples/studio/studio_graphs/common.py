"""Shared helpers for the Studio example graphs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class SchemaOutOfDateError(RuntimeError):
    """Raised when persisted state predates the current schema version.

    This is the error you are *supposed* to see in Studio during the demo: it means
    a thread (or store item) was written by schema v1 and the v2 code found the new
    fields missing. Enabling LangMigrate (``LANGMIGRATE_ENABLED = True`` in the
    graph's ``graph.py``) makes it disappear.
    """


def last_human_text(messages: Sequence[Any]) -> str:
    """Return the text of the most recent human message (best-effort)."""
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            content = message.content
            if isinstance(content, str):
                return content
            # Studio may send content blocks; keep only the text parts.
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return " ".join(p for p in parts if p)
            return str(content)
    return ""
