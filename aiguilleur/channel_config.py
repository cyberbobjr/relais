"""Channel configuration for the unified AIGUILLEUR process.

Defines the ChannelConfig frozen dataclass and load_channels_config() function.
Configuration cascade: ~/.relais/config/ > /opt/relais/config/ > ./config/
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from common.config_loader import resolve_config_path

_CHANNELS_CONFIG_FILE = "aiguilleur.yaml"


class ProfileRef:
    """Thread-safe mutable holder for a channel's active LLM profile.

    Wraps a single ``str | None`` behind a lock so that the hot-reload
    thread can update the profile atomically while adapter threads read
    it concurrently.  The object is placed inside a frozen ``ChannelConfig``
    — Python allows a mutable object inside a frozen dataclass because
    the dataclass only prevents reassignment of the *field*, not mutation
    of the *object* the field points to.

    Args:
        profile: Initial profile name (e.g. ``"fast"``), or ``None``.
    """

    def __init__(self, profile: str | None) -> None:
        self._profile = profile
        self._lock = threading.Lock()

    @property
    def profile(self) -> str | None:
        """Return the current profile name, thread-safely.

        Returns:
            The current profile string, or ``None`` when unset.
        """
        with self._lock:
            return self._profile

    def update(self, new_profile: str | None) -> None:
        """Replace the stored profile name, thread-safely.

        Args:
            new_profile: The new profile name, or ``None`` to unset.
        """
        with self._lock:
            self._profile = new_profile


# Sentinel: ``ChannelConfig.__post_init__`` uses identity check (``is``) to
# detect when no explicit ``profile_ref`` was provided by the caller.
_NO_PROFILE_REF: ProfileRef = ProfileRef(None)


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
        profile_ref:  Thread-safe mutable reference to the active LLM profile.
                      Automatically initialised from ``profile`` in ``__post_init__``.
                      The hot-reload path calls ``profile_ref.update()`` so adapters
                      always read the latest value without a restart.
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
    profile_ref: ProfileRef = field(default=_NO_PROFILE_REF, compare=False, hash=False)

    def __post_init__(self) -> None:
        """Sync profile_ref with profile when constructing without an explicit profile_ref.

        When a caller does ``ChannelConfig(name="discord", profile="fast")`` the
        default is the module-level ``_NO_PROFILE_REF`` sentinel.  This hook
        detects that via an identity check and replaces it with
        ``ProfileRef(profile)`` so that ``profile_ref.profile == profile``
        without requiring the caller to pass both arguments explicitly.

        If an explicit ``profile_ref`` is passed (identity-preserving reload
        path), the ``is not _NO_PROFILE_REF`` guard leaves it untouched.
        """
        if self.profile_ref is _NO_PROFILE_REF:
            object.__setattr__(self, "profile_ref", ProfileRef(self.profile))


def _parse_int(value: object, default: int) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def load_channels_config() -> dict[str, ChannelConfig]:
    """Load channel configurations from aiguilleur.yaml.

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
