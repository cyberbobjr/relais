"""ProgressConfig — runtime configuration for Atelier progress event publishing.

Loaded from atelier.yaml in the standard config cascade.  Controls whether
progress events (tool calls, tool results, subagent starts) are published to
the streaming stream and optionally to the non-streaming outgoing stream.
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
}


@dataclass(frozen=True)
class ProgressConfig:
    """Configuration for progress event publishing in the Atelier brick.

    Attributes:
        enabled: Master switch — False disables all progress publishing.
        events: Per-event enable flags. Keys are event names
            (``"tool_call"``, ``"tool_result"``, ``"subagent_start"``).
        publish_to_outgoing: When True, also publish progress events to
            ``relais:messages:outgoing:{channel}`` for non-streaming adapters.
        detail_max_length: Maximum character length for the ``detail`` field.
            Detail strings longer than this are truncated before publishing.
    """

    enabled: bool = True
    events: dict[str, bool] = field(default_factory=lambda: dict(_DEFAULT_EVENTS))
    publish_to_outgoing: bool = True
    detail_max_length: int = 100


def load_progress_config() -> ProgressConfig:
    """Load the progress configuration from atelier.yaml.

    Walks the standard config cascade: ~/.relais/config/ > /opt/relais/config/
    > ./config/.  Returns default values silently when atelier.yaml is absent.

    Returns:
        ProgressConfig populated from the YAML file, or a default instance
        if the file is not found.
    """
    try:
        path = resolve_config_path("atelier.yaml")
        raw: dict = yaml.safe_load(path.read_text()) or {}
        progress = raw.get("progress", {})
        events_raw = progress.get("events", {})
        events = {**_DEFAULT_EVENTS, **events_raw}
        return ProgressConfig(
            enabled=bool(progress.get("enabled", True)),
            events=events,
            publish_to_outgoing=bool(progress.get("publish_to_outgoing", True)),
            detail_max_length=int(progress.get("detail_max_length", 100)),
        )
    except FileNotFoundError:
        logger.debug("atelier.yaml not found — using default progress config")
        return ProgressConfig()
