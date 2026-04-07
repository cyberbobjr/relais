"""ErrorSynthesizer — produce a user-visible error message from a failed agent turn.

When an ``AgentExecutionError`` is caught by Atelier, the agent loop ended
abnormally (e.g. too many consecutive tool errors).  Instead of silently
dropping the request, we perform a lightweight LLM call that examines the
partial conversation history and generates an empathetic, actionable reply
for the user.
"""

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from common.profile_loader import ProfileConfig

logger = logging.getLogger("atelier.error_synthesizer")

_SYSTEM_PROMPT = (
    "You are a helpful assistant. The agent encountered an unrecoverable error "
    "while processing the user's request. Based on the conversation history below, "
    "write a short, empathetic reply (2-4 sentences) that:\n"
    "1. Acknowledges what the user asked for.\n"
    "2. Honestly explains that you ran into a technical problem.\n"
    "3. Suggests a concrete next step when possible (e.g. check the configuration, "
    "   try again later, or contact support).\n"
    "Reply only with the message to show the user — no meta-commentary."
)

_FALLBACK_MESSAGE = (
    "I'm sorry, I ran into an unexpected technical problem while processing your "
    "request and was unable to complete it. Please try again, or contact support "
    "if the issue persists."
)


class ErrorSynthesizer:
    """Produce a user-visible error reply from a failed agent turn.

    Uses the same LLM profile as the original request so that the error
    message stays consistent with the configured model/temperature.
    """

    async def synthesize(
        self,
        messages_raw: list[dict],
        error: str,
        profile: ProfileConfig,
    ) -> str:
        """Generate an error explanation for the user.

        Args:
            messages_raw: Partial conversation history from the failed turn
                (serialised LangChain message dicts).
            error: String representation of the exception.
            profile: LLM profile to use for the synthesis call.

        Returns:
            A non-empty string suitable for sending to the user.
        """
        try:
            llm = init_chat_model(profile.model, temperature=0)
            prompt: list[Any] = [SystemMessage(content=_SYSTEM_PROMPT)]

            # Reconstruct conversation context from the partial history.
            for msg in messages_raw:
                role = msg.get("type") or msg.get("role", "")
                content = msg.get("content", "")
                if role in ("human", "user"):
                    prompt.append(HumanMessage(content=str(content)))
                # We intentionally skip AI/tool messages — they are context
                # for the synthesizer's system prompt, not part of the prompt chain.

            response = await llm.ainvoke(prompt)
            text = getattr(response, "content", "") or ""
            if text.strip():
                return text.strip()
            return _FALLBACK_MESSAGE

        except Exception as exc:
            logger.warning("ErrorSynthesizer LLM call failed: %s", exc)
            return _FALLBACK_MESSAGE
