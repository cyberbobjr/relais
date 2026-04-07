"""IntentLabeler — extracts an intent label from a session via Haiku LLM.

Uses a cheap Haiku LLM call to classify the primary recurring task type of a
session into a normalized snake_case label, or returns None for generic chat.
"""

from __future__ import annotations

import logging
import re
from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from common.profile_loader import ProfileConfig, build_chat_model

logger = logging.getLogger(__name__)

# Reserved labels that must not trigger skill creation
_EXCLUDED_LABELS = frozenset({"none", "unknown", "general", "chat", "conversation", "question"})

# Regex to validate that a label is valid snake_case (2-40 chars)
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class IntentLabelLLMResponse(BaseModel):
    """Structured output schema for the intent labeling LLM call."""

    label: str = Field(
        description=(
            "Single snake_case intent label identifying the primary recurring task "
            "(e.g. send_email, summarize_pdf, search_web, create_calendar_event). "
            "Use 'none' if the conversation has no clear reusable task."
        )
    )


class IntentLabeler:
    """Extract a normalized intent label from a session's messages_raw.

    Uses a cheap LLM call (Haiku via annotation_profile) to classify the
    session's primary task type into a short snake_case label suitable for
    grouping into a skill.

    Args:
        profile: The annotation ProfileConfig (typically "fast" = Haiku).
    """

    _SYSTEM_PROMPT = (
        "You are a task classifier. Given a conversation, identify the single "
        "primary recurring task type it represents as a short snake_case label "
        "(e.g. send_email, summarize_pdf, search_web, create_calendar_event). "
        "If the conversation is generic chat or has no clear reusable task, "
        "use 'none'."
    )

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile

    def _extract_user_messages(self, messages_raw: list[dict]) -> list[str]:
        """Extract only HumanMessage content from a serialized message list.

        Handles both LangChain serialization styles:
        - ``type="human"``
        - ``id=[..., "HumanMessage"]``

        Args:
            messages_raw: Deserialized LangChain message list (list of dicts).

        Returns:
            List of user message content strings (stripped, max 300 chars each).
        """
        user_msgs: list[str] = []
        for msg in messages_raw:
            msg_type = msg.get("type", "")
            is_human = msg_type == "human" or (
                isinstance(msg.get("id"), list) and "HumanMessage" in str(msg.get("id"))
            )
            if is_human:
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_msgs.append(content.strip()[:300])
        return user_msgs

    async def label(self, messages_raw: list[dict]) -> str | None:
        """Extract an intent label from a session's messages.

        Args:
            messages_raw: Full serialized LangChain message list for the turn.

        Returns:
            Normalized snake_case intent label, or None if no clear intent.
        """
        user_messages = self._extract_user_messages(messages_raw)
        if not user_messages:
            logger.debug("IntentLabeler: no user messages found in session")
            return None

        conversation_text = "\n".join(f"- {m}" for m in user_messages[:5])

        try:
            model = build_chat_model(self._profile)
            structured_model = model.with_structured_output(IntentLabelLLMResponse)
            result = cast(IntentLabelLLMResponse, await structured_model.ainvoke([
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=f"Conversation:\n{conversation_text}"),
            ]))
            raw_label = result.label.strip().lower()
        except Exception as exc:  # noqa: BLE001
            logger.warning("IntentLabeler LLM call failed: %s", exc)
            return None

        if not _LABEL_RE.match(raw_label):
            logger.debug("IntentLabeler: invalid label format '%s'", raw_label)
            return None
        if raw_label in _EXCLUDED_LABELS:
            logger.debug("IntentLabeler: excluded label '%s'", raw_label)
            return None

        logger.info("IntentLabeler: session → label='%s'", raw_label)
        return raw_label
