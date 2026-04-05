"""Channel configuration for the unified AIGUILLEUR process.

Defines the ChannelConfig frozen dataclass and load_channels_config() function.
Configuration cascade: ~/.relais/config/ > /opt/relais/config/ > ./config/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

_CHANNELS_CONFIG_FILE = "channels.yaml"


@dataclass(frozen=True)
class ChannelConfig:
    """Immutable configuration for a single channel adapter.

    Attributes:
        name:         Channel identifier (e.g. 'discord').
        enabled:      Whether the adapter is started by the Aiguilleur process.
        streaming:    Whether the channel supports real-time chunk rendering.
        type:         'native' (Python thread) or 'external' (subprocess).
        command:      For type=external: the executable to spawn.
        args:         For type=external: command-line arguments.
        class_path:   Optional fully-qualified Python class path override.
                      Defaults to aiguilleur.channels.{name}.adapter.*Aiguilleur.
        max_restarts: Max automatic restart attempts on crash. Default 5.
        profile:      Optional LLM profile name (e.g. 'fast', 'precise').
                      When set, the Aiguilleur stamps
                      envelope.context["aiguilleur"]["channel_profile"]
                      with this value, overriding config.yaml:llm.default_profile.
                      None means fall back to the system default profile.
        prompt_path:  Optional relative path (relative to prompts_dir) to the
                      channel formatting overlay.  When set, the Aiguilleur stamps
                      envelope.context["aiguilleur"]["channel_prompt_path"] with
                      this value so that Atelier can load it via soul_assembler.
                      None means no channel overlay is loaded.
    """

    name: str
    enabled: bool = True
    streaming: bool = False
    type: str = "native"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    class_path: str | None = None
    max_restarts: int = 5
    profile: str | None = None
    prompt_path: str | None = None


def _parse_int(value: object, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def load_channels_config() -> dict[str, ChannelConfig]:
    """Load channel configurations from channels.yaml.

    Returns a dict keyed by channel name. Falls back to a minimal
    discord-enabled config when the file is not found.

    Returns:
        dict[str, ChannelConfig]: Channel configurations keyed by name.
    """
    try:
        config_path: Path = resolve_config_path(_CHANNELS_CONFIG_FILE)
        raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError:
        return {
            "discord": ChannelConfig(name="discord", enabled=True, streaming=True)
        }

    channels_raw: dict[str, Any] = raw.get("channels", {}) or {}

    result: dict[str, ChannelConfig] = {}
    for name, values in channels_raw.items():
        values = values or {}
        result[name] = ChannelConfig(
            name=name,
            enabled=bool(values.get("enabled", True)),
            streaming=bool(values.get("streaming", False)),
            type=str(values.get("type", "native")),
            command=values.get("command") or None,
            args=list(values.get("args") or []),
            class_path=values.get("class") or None,
            max_restarts=_parse_int(values.get("max_restarts", 5), default=5),
            profile=values.get("profile") or None,
            prompt_path=values.get("prompt_path") or None,
        )

    return result
