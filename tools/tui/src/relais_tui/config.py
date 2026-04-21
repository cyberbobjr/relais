"""Configuration module for RELAIS TUI.

Handles loading, saving, and defaulting of the TUI configuration
from a YAML file with environment variable overrides.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import yaml

_ENV_API_KEY = "RELAIS_TUI_API_KEY"
_ENV_RELAIS_HOME = "RELAIS_HOME"
_FALLBACK_HOME = Path("~/.relais")


def _default_config_path() -> Path:
    """Resolve the default config file path.

    Follows the RELAIS config cascade convention: config files live under
    ``<relais_home>/config/``.  Uses ``RELAIS_HOME`` env var if set,
    otherwise falls back to ``~/.relais``.

    Returns:
        Path to ``<relais_home>/config/tui/config.yaml``.
    """
    home = os.environ.get(_ENV_RELAIS_HOME)
    if home:
        return Path(home) / "config" / "tui" / "config.yaml"
    return _FALLBACK_HOME / "config" / "tui" / "config.yaml"


@dataclass(frozen=True)
class ThemeConfig:
    """Theme color configuration for the TUI.

    All values are CSS-compatible color strings. Missing keys in YAML
    fall back to the defaults defined here.
    """

    background: str = "#1a1a2e"
    user_text: str = "#8be9fd"
    assistant_text: str = "#f8f8f2"
    code_block: str = "#282a36"
    progress: str = "#6272a4"
    error: str = "#ff5555"
    metadata: str = "#6272a4"
    status_bar: str = "#16213e"
    accent: str = "#50fa7b"


@dataclass(frozen=True)
class FontConfig:
    """Terminal font hint. The actual font must be configured in your terminal emulator
    (e.g. iTerm2 → Preferences → Profiles → Text, Alacritty → font.family).
    These fields are documentation only — the TUI cannot change the terminal font.
    """

    family: str = ""  # e.g. "JetBrains Mono", "Fira Code"
    size: int = 0     # e.g. 14


@dataclass(frozen=True)
class Config:
    """TUI configuration.

    Immutable after construction. Use ``load_config`` to build from YAML
    and ``save_config`` to persist.
    """

    api_url: str = "http://localhost:8080"
    api_key: str = ""
    history_path: str = "~/.relais/storage/tui/history"
    request_timeout: int = 120
    last_session_id: str = ""
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    font: FontConfig = field(default_factory=FontConfig)


def load_config(path: Path | None = None) -> Config:
    """Load configuration from a YAML file.

    If the file does not exist, a default config is written with ``0o600``
    permissions. Missing keys in the file are filled with defaults.
    The environment variable ``RELAIS_TUI_API_KEY``, when set and non-empty,
    overrides the ``api_key`` field from the file.

    Args:
        path: Path to the YAML config file. Defaults to
            ``~/.relais/config/tui/config.yaml``.

    Returns:
        A frozen Config instance.
    """
    resolved = (path or _default_config_path()).expanduser()

    if not resolved.exists():
        cfg = Config()
        save_config(cfg, resolved)
        return _apply_env(cfg)

    raw = yaml.safe_load(resolved.read_text()) or {}
    cfg = _build_config(raw)
    return _apply_env(cfg)


def save_config(config: Config, path: Path | None = None) -> None:
    """Save configuration to a YAML file with ``0o600`` permissions.

    Creates parent directories if they do not exist.

    Args:
        config: The Config instance to persist.
        path: Destination path. Defaults to ``~/.relais/config/tui/config.yaml``.
    """
    resolved = (path or _default_config_path()).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(config)
    resolved.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    resolved.chmod(0o600)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_config(raw: dict) -> Config:
    """Build a Config from a raw YAML dict, applying defaults for missing keys.

    Args:
        raw: Parsed YAML dictionary (may be partial).

    Returns:
        A fully populated Config instance.
    """
    theme_raw = raw.get("theme") or {}
    theme_defaults = ThemeConfig()
    theme = ThemeConfig(**{
        k: theme_raw.get(k, getattr(theme_defaults, k))
        for k in ThemeConfig.__dataclass_fields__
    })

    font_raw = raw.get("font") or {}
    font_defaults = FontConfig()
    font = FontConfig(**{
        k: font_raw.get(k, getattr(font_defaults, k))
        for k in FontConfig.__dataclass_fields__
    })

    known_fields = Config.__dataclass_fields__
    defaults = Config()
    kwargs: dict = {}
    for key in known_fields:
        if key == "theme":
            kwargs["theme"] = theme
        elif key == "font":
            kwargs["font"] = font
        elif key in raw:
            kwargs[key] = raw[key]
        else:
            kwargs[key] = getattr(defaults, key)

    return Config(**kwargs)


def _apply_env(cfg: Config) -> Config:
    """Override api_key from environment variable if set and non-empty.

    Args:
        cfg: The Config to potentially override.

    Returns:
        A new Config with the env-based api_key, or the original unchanged.
    """
    env_key = os.environ.get(_ENV_API_KEY, "")
    if env_key:
        return replace(cfg, api_key=env_key)
    return cfg
