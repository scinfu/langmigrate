"""Offline stand-in chat model so the agent graph runs without an API key.

It echoes the last human message and never calls tools. Set ``ANTHROPIC_API_KEY``
to use a real Claude model instead (see ``graph.py``).
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class EchoToolModel(BaseChatModel):
    """Deterministic echo model that tolerates ``bind_tools`` (returns itself)."""

    @property
    def _llm_type(self) -> str:
        return "echo-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> EchoToolModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = next((m for m in reversed(messages) if m.type == "human"), None)
        text = "" if last is None else str(last.content)
        reply = AIMessage(
            content=(
                f"[offline echo model — set ANTHROPIC_API_KEY for a real LLM] You said: {text!r}"
            )
        )
        return ChatResult(generations=[ChatGeneration(message=reply)])
