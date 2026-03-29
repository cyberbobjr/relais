"""Content guardrails for La Sentinelle — pre/post LLM input/output filtering."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger("sentinelle.guardrails")

_DEFAULT_MAX_INPUT_LENGTH: int = 4000
_DEFAULT_MAX_OUTPUT_LENGTH: int = 8000

_BUILTIN_INPUT_PATTERNS: list[str] = [
    r"(?i)ignore\s+(all\s+)?previous\s+instructions",
    r"(?i)you\s+are\s+now\s+DAN",
    r"(?i)jailbreak",
]

# ---------------------------------------------------------------------------
# Profile-based guardrail rule definitions
# ---------------------------------------------------------------------------

# Patterns that indicate shell/code execution attempts.
_NO_CODE_EXEC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"`[^`]+`"),             # backtick execution: `cmd`
    re.compile(r"\$\("),                # subshell: $(...)
    re.compile(r"\bsubprocess\b"),      # subprocess module reference
    re.compile(r"\bos\.system\s*\("),   # os.system(...)
    re.compile(r"\bexec\s*\("),         # exec(...)
    re.compile(r"\beval\s*\("),         # eval(...)
]

# Patterns that match external URLs.
_NO_EXTERNAL_LINKS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://"),           # http:// or https://
]

_PROFILE_RULE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "no_code_exec": _NO_CODE_EXEC_PATTERNS,
    "no_external_links": _NO_EXTERNAL_LINKS_PATTERNS,
}


@dataclass(frozen=True)
class GuardrailResult:
    """Result returned by a guardrail check.

    Attributes:
        allowed: Whether the text is permitted to proceed.
        reason: Human-readable explanation when not allowed, None otherwise.
        modified_text: Replacement text when content is partially censored;
            None when the original text is unchanged.
    """

    allowed: bool
    reason: str | None = None
    modified_text: str | None = None


class ContentFilter:
    """Pre/post LLM content filters loaded from guardrails.yaml.

    Falls back to built-in defaults when no config file is found.

    Args:
        config_path: Optional explicit path to guardrails.yaml.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._max_input_length: int = _DEFAULT_MAX_INPUT_LENGTH
        self._max_output_length: int = _DEFAULT_MAX_OUTPUT_LENGTH
        self._input_patterns: list[re.Pattern[str]] = []
        self._output_patterns: list[re.Pattern[str]] = []
        self._load(self._resolve_path(config_path))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_input(self, text: str, user_id: str) -> GuardrailResult:
        """Validates an incoming message before it is sent to the LLM.

        Checks: (1) length limit — hard block; (2) dangerous patterns — hard block.

        Args:
            text: Raw user message content.
            user_id: Sender identifier for logging context.

        Returns:
            GuardrailResult with allowed=True when all checks pass.
        """
        if len(text) > self._max_input_length:
            reason = f"Input too long: {len(text)} chars (max {self._max_input_length})"
            logger.warning("Guardrail [input/length] blocked %s: %s", user_id, reason)
            return GuardrailResult(allowed=False, reason=reason)

        for pattern in self._input_patterns:
            if pattern.search(text):
                reason = f"Blocked by content policy (pattern: {pattern.pattern!r})"
                logger.warning("Guardrail [input/pattern] blocked %s: %s", user_id, reason)
                return GuardrailResult(allowed=False, reason=reason)

        return GuardrailResult(allowed=True)

    async def check_output(self, text: str, user_id: str) -> GuardrailResult:
        """Validates an LLM response before it is delivered to the user.

        Checks: (1) length limit — soft truncation with notice;
        (2) dangerous patterns — hard block.

        Args:
            text: LLM-generated response content.
            user_id: Recipient identifier for logging context.

        Returns:
            GuardrailResult with allowed=True when all checks pass.
            modified_text is set when the response was truncated.
        """
        modified: str | None = None

        if len(text) > self._max_output_length:
            truncated = text[: self._max_output_length]
            modified = truncated + "\n\n[Response truncated by content policy.]"
            logger.info(
                "Guardrail [output/length] truncated response for %s (%d → %d chars)",
                user_id, len(text), self._max_output_length,
            )
            text = modified

        for pattern in self._output_patterns:
            if pattern.search(text):
                reason = f"Output blocked by content policy (pattern: {pattern.pattern!r})"
                logger.warning("Guardrail [output/pattern] blocked for %s: %s", user_id, reason)
                return GuardrailResult(allowed=False, reason=reason)

        return GuardrailResult(allowed=True, modified_text=modified)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, config_path: Path | None) -> Path | None:
        """Returns the first usable guardrails config file path.

        Args:
            config_path: Caller-supplied path, or None for auto-discovery.

        Returns:
            Resolved Path if a file exists, otherwise None.
        """
        if config_path is not None:
            return config_path if config_path.exists() else None

        for filename in ("guardrails.yaml", "guardrails.yaml.default"):
            try:
                return resolve_config_path(filename)
            except FileNotFoundError:
                continue

        return None

    def _load(self, config_path: Path | None) -> None:
        """Parses guardrails.yaml and compiles regex patterns.

        Falls back to built-in defaults when config_path is None or unreadable.

        Args:
            config_path: Resolved path to guardrails.yaml, or None.
        """
        cfg: dict[str, Any] = {}

        if config_path is not None:
            try:
                cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                logger.info("Guardrails config loaded from %s", config_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Guardrails: failed to parse %s — %s — using built-in defaults",
                    config_path, exc,
                )
        else:
            logger.debug(
                "Guardrails: no config file found in any config search path — using built-in defaults"
            )

        self._max_input_length = int(cfg.get("max_input_length", _DEFAULT_MAX_INPUT_LENGTH))
        self._max_output_length = int(cfg.get("max_output_length", _DEFAULT_MAX_OUTPUT_LENGTH))
        self._input_patterns = self._compile_patterns(
            cfg.get("input_patterns", _BUILTIN_INPUT_PATTERNS), "input"
        )
        self._output_patterns = self._compile_patterns(
            cfg.get("output_patterns", []), "output"
        )

    @staticmethod
    def _compile_patterns(patterns: list[str], context: str) -> list[re.Pattern[str]]:
        """Compiles regex strings, skipping invalid ones with a warning.

        Args:
            patterns: List of regex pattern strings to compile.
            context: Label used in log messages for diagnostics.

        Returns:
            List of compiled re.Pattern objects.
        """
        compiled: list[re.Pattern[str]] = []
        for raw in patterns:
            try:
                compiled.append(re.compile(raw))
            except re.error as exc:
                logger.warning("Guardrails: invalid %s pattern %r — skipped (%s)", context, raw, exc)
        return compiled


# ---------------------------------------------------------------------------
# Profile-based guardrail checker
# ---------------------------------------------------------------------------


class ProfileGuardrails:
    """Applies content rules derived from a user's ProfileConfig.

    Each rule in ``profile.guardrails`` maps to a set of pre-compiled regex
    patterns. Rules are evaluated in order; the first match short-circuits
    evaluation and returns a rejected ``GuardrailResult``.

    Args:
        profile: The ProfileConfig whose ``guardrails`` tuple drives rule selection.

    Raises:
        ValueError: A rule name listed in the profile has no registered patterns.
    """

    def __init__(self, profile: Any) -> None:
        """Initialises ProfileGuardrails by resolving rule patterns from the profile.

        Args:
            profile: A ProfileConfig instance with a ``guardrails`` attribute of
                type ``tuple[str, ...]``.

        Raises:
            ValueError: An entry in ``profile.guardrails`` is not a recognised
                guardrail rule name.
        """
        self._rules: list[tuple[str, list[re.Pattern[str]]]] = []
        for rule_name in profile.guardrails:
            patterns = _PROFILE_RULE_PATTERNS.get(rule_name)
            if patterns is None:
                raise ValueError(
                    f"ProfileGuardrails: unknown guardrail rule '{rule_name}'. "
                    f"Supported rules: {sorted(_PROFILE_RULE_PATTERNS)}"
                )
            self._rules.append((rule_name, patterns))

    async def check(self, text: str, user_id: str) -> GuardrailResult:
        """Evaluates all profile-derived guardrail rules against the message text.

        Rules are tested in the order they appear in the profile's ``guardrails``
        tuple. Evaluation stops at the first rule that matches.

        Args:
            text: The message content to evaluate.
            user_id: Sender identifier used for log context.

        Returns:
            A frozen ``GuardrailResult``. ``allowed=True`` when no rule fires;
            ``allowed=False`` with a populated ``reason`` on the first match.
        """
        for rule_name, patterns in self._rules:
            for pattern in patterns:
                if pattern.search(text):
                    reason = (
                        f"Message blocked by profile guardrail '{rule_name}' "
                        f"(pattern: {pattern.pattern!r})"
                    )
                    logger.warning(
                        "ProfileGuardrail [%s] blocked message from %s: %s",
                        rule_name,
                        user_id,
                        reason,
                    )
                    return GuardrailResult(allowed=False, reason=reason)

        return GuardrailResult(allowed=True)
