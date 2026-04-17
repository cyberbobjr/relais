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


def load_display_config() -> DisplayConfig:
    """Load the display configuration from atelier.yaml.

    Walks the standard config cascade (``~/.relais/config/``).
    Logs at DEBUG level and returns default values when atelier.yaml is absent.

    Returns:
        DisplayConfig populated from the YAML file, or a default instance
        if the file is not found.
    """
    try:
        path = resolve_config_path("atelier.yaml")
        raw: dict = yaml.safe_load(path.read_text()) or {}
        display = raw.get("display", {})
        events_raw = display.get("events", {})
        events = {**_DEFAULT_EVENTS, **events_raw}
        return DisplayConfig(
            enabled=bool(display.get("enabled", True)),
            final_only=bool(display.get("final_only", True)),
            detail_max_length=int(display.get("detail_max_length", 100)),
            events=events,
        )
    except FileNotFoundError:
        logger.debug("atelier.yaml not found — using default display config")
        return DisplayConfig()
    except (TypeError, ValueError) as exc:
        logger.warning("atelier.yaml display section has invalid value — using defaults: %s", exc)
        return DisplayConfig()
