"""DisplayConfig — runtime configuration for Atelier display event publishing.

Loaded from atelier.yaml in the standard config cascade. Controls whether
progress events and LLM output tokens are published to the channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

import yaml

from common.config_loader import resolve_config_path

logger = logging.getLogger(__name__)

_DEFAULT_EVENTS: Final[dict[str, bool]] = {
    "tool_call": True,
    "tool_result": True,
    "subagent_start": True,
    "thinking": False,
}


@dataclass(frozen=True)
class DisplayConfig:
    """Configuration for display event publishing in the Atelier brick.

    Attributes:
        enabled: Master switch — False disables all event publishing.
        final_only: When True, only the final LLM reply (text after the last
            tool call) is transmitted. Pre-tool narration tokens are discarded.
        detail_max_length: Maximum character length for the detail field.
            Detail strings longer than this are truncated before publishing.
        events: Per-event enable flags. Keys are event names
            (tool_call, tool_result, subagent_start, thinking).
    """

    enabled: bool = True
    final_only: bool = True
    detail_max_length: int = 100
    events: dict[str, bool] = field(default_factory=lambda: dict(_DEFAULT_EVENTS))


def _validate_bool(field_name: str, raw: object, default: bool) -> bool:
    """Coerce *raw* to bool, warning on unexpected types.

    Args:
        field_name: YAML key name, used in the warning message.
        raw: Value read from YAML (may be any type).
        default: Fallback value returned when *raw* cannot be coerced.

    Returns:
        Coerced bool value, or *default* if coercion failed.
    """
    if not isinstance(raw, bool):
        logger.warning(
            "display_config: champ '%s' invalide (reçu %r, attendu %s), valeur par défaut utilisée",
            field_name,
            raw,
            "bool",
        )
        return default
    return raw


def _validate_int(field_name: str, raw: object, default: int, min_val: int = 0) -> int:
    """Coerce *raw* to int and check lower bound, warning on failure.

    Args:
        field_name: YAML key name, used in the warning message.
        raw: Value read from YAML (may be any type).
        default: Fallback value returned when coercion or bounds check fails.
        min_val: Minimum accepted value (inclusive).

    Returns:
        Coerced int value, or *default* if validation failed.
    """
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "display_config: champ '%s' invalide (reçu %r, attendu %s), valeur par défaut utilisée",
            field_name,
            raw,
            "int",
        )
        return default
    if value < min_val:
        logger.warning(
            "display_config: champ '%s' invalide (reçu %r, attendu int >= %d), valeur par défaut utilisée",
            field_name,
            raw,
            min_val,
        )
        return default
    return value


def load_display_config() -> DisplayConfig:
    """Load the display configuration from atelier.yaml.

    Walks the standard config cascade (``~/.relais/config/``).
    Validates each field individually: invalid fields emit a WARNING and fall back
    to their default value while the rest of the configuration is still applied.
    Logs at DEBUG level and returns a fully-default instance when atelier.yaml is absent.

    Returns:
        DisplayConfig populated from the YAML file, with per-field defaults applied
        for any invalid values.
    """
    try:
        path = resolve_config_path("atelier.yaml")
        raw: dict = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        logger.debug("atelier.yaml not found — using default display config")
        return DisplayConfig()

    display = raw.get("display", {}) if isinstance(raw.get("display"), dict) else {}

    enabled = _validate_bool("enabled", display.get("enabled", True), default=True)
    final_only = _validate_bool("final_only", display.get("final_only", True), default=True)
    detail_max_length = _validate_int(
        "detail_max_length",
        display.get("detail_max_length", 100),
        default=100,
        min_val=0,
    )

    events_raw = display.get("events", {})
    events: dict[str, bool] = dict(_DEFAULT_EVENTS)
    if isinstance(events_raw, dict):
        for key, val in events_raw.items():
            events[key] = _validate_bool(f"events.{key}", val, default=_DEFAULT_EVENTS.get(key, False))

    return DisplayConfig(
        enabled=enabled,
        final_only=final_only,
        detail_max_length=detail_max_length,
        events=events,
    )
