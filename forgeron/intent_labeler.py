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
    is_correction: bool = Field(
        default=False,
        description=(
            "True if the user is correcting or criticizing a previous AI response "
            "(e.g. 'that was wrong', 'do it differently', 'stop doing X'). "
            "False for normal task requests."
        ),
    )
    corrected_behavior: str | None = Field(
        default=None,
        description=(
            "When is_correction=True: concise description of what the AI should do "
            "differently (e.g. 'Use plain text instead of HTML'). "
            "Null when is_correction=False."
        ),
    )
    skill_name_hint: str | None = Field(
        default=None,
        description=(
            "Optional snake_case skill name hint derived from the correction context "
            "(e.g. 'send_plain_email'). Used by Forgeron to name the corrected skill. "
            "Null when no specific skill is implied."
        ),
    )


class IntentLabelResult:
    """Result of an IntentLabeler.label() call.

    Carries both the intent label (for skill auto-creation) and correction
    metadata (for skill-designer dispatch via the correction pipeline).

    Attributes:
        label: Normalized snake_case intent label, or None if excluded/unknown.
        is_correction: True if the session is a user correction of prior AI behavior.
        corrected_behavior: Human-readable description of the correction (non-null iff is_correction).
        skill_name_hint: Optional snake_case skill name suggested by the LLM.
    """

    __slots__ = ("label", "is_correction", "corrected_behavior", "skill_name_hint")

    def __init__(
        self,
        label: str | None,
        is_correction: bool,
        corrected_behavior: str | None,
        skill_name_hint: str | None,
    ) -> None:
        self.label = label
        self.is_correction = is_correction
        self.corrected_behavior = corrected_behavior
        self.skill_name_hint = skill_name_hint


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

    async def label(self, messages_raw: list[dict]) -> IntentLabelResult:
        """Extract an intent label and correction metadata from a session's messages.

        Args:
            messages_raw: Full serialized LangChain message list for the turn.

        Returns:
            IntentLabelResult with label (or None if excluded/unknown) and
            correction metadata (is_correction, corrected_behavior, skill_name_hint).
        """
        user_messages = self._extract_user_messages(messages_raw)
        if not user_messages:
            logger.debug("IntentLabeler: no user messages found in session")
            return IntentLabelResult(
                label=None, is_correction=False,
                corrected_behavior=None, skill_name_hint=None,
            )

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
            return IntentLabelResult(
                label=None, is_correction=False,
                corrected_behavior=None, skill_name_hint=None,
            )

        # Validate label format — excluded/invalid labels become None, but
        # correction metadata is always forwarded regardless.
        normalized_label: str | None
        if not _LABEL_RE.match(raw_label) or raw_label in _EXCLUDED_LABELS:
            if not _LABEL_RE.match(raw_label):
                logger.debug("IntentLabeler: invalid label format '%s'", raw_label)
            else:
                logger.debug("IntentLabeler: excluded label '%s'", raw_label)
            normalized_label = None
        else:
            logger.info("IntentLabeler: session → label='%s'", raw_label)
            normalized_label = raw_label

        return IntentLabelResult(
            label=normalized_label,
            is_correction=result.is_correction,
            corrected_behavior=result.corrected_behavior,
            skill_name_hint=result.skill_name_hint,
        )
