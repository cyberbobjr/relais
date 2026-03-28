"""Profile loader for the Atelier brick.

Reads LLM profiles from a YAML configuration file following the standard
config cascade: ~/.relais/config/ > /opt/relais/config/ > ./config/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResilienceConfig:
    """Resilience parameters for a single LLM profile.

    Attributes:
        retry_attempts: Maximum number of retry attempts on transient failure.
        retry_delays: Progressive backoff delays in seconds between retries.
        fallback_model: Optional model identifier to use when all retries fail.
    """

    retry_attempts: int
    retry_delays: list[int]
    fallback_model: str | None = None


@dataclass(frozen=True)
class ProfileConfig:
    """Configuration for a single named LLM profile.

    Attributes:
        model: LiteLLM model identifier (e.g. "mistral-small-2603").
        temperature: Sampling temperature controlling response randomness.
        max_tokens: Maximum number of tokens the LLM may generate.
        resilience: Retry and fallback configuration for transient failures.
    """

    model: str
    temperature: float
    max_tokens: int
    resilience: ResilienceConfig


# ---------------------------------------------------------------------------
# Config cascade paths
# ---------------------------------------------------------------------------

_CASCADE_DIRS: list[Path] = [
    Path.home() / ".relais" / "config",
    Path("/opt/relais/config"),
    Path("./config"),
]

_FILENAME = "profiles.yaml"


def _find_config_file() -> Path:
    """Locate the first profiles.yaml in the config cascade.

    Returns:
        Path to the first existing profiles.yaml found in the cascade.

    Raises:
        FileNotFoundError: No profiles.yaml found in any cascade directory.
    """
    for directory in _CASCADE_DIRS:
        candidate = directory / _FILENAME
        if candidate.exists():
            return candidate

    searched = ", ".join(str(d / _FILENAME) for d in _CASCADE_DIRS)
    raise FileNotFoundError(
        f"profiles.yaml not found in config cascade. Searched: {searched}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_profiles(
    config_path: str | Path | None = None,
) -> dict[str, ProfileConfig]:
    """Load all LLM profiles from a YAML configuration file.

    When config_path is given, reads that file directly (useful for tests).
    Otherwise, walks the standard cascade: ~/.relais/config/ > /opt/relais/config/
    > ./config/.

    Args:
        config_path: Optional explicit path to a profiles YAML file. When
            provided, the config cascade is bypassed entirely.

    Returns:
        Dictionary mapping profile name strings to ProfileConfig instances.

    Raises:
        FileNotFoundError: config_path provided but does not exist, or no
            profiles.yaml found in the config cascade.
        KeyError: The YAML file is missing the top-level 'profiles' key.
        yaml.YAMLError: The file content is not valid YAML.
    """
    if config_path is not None:
        resolved = Path(config_path)
    else:
        resolved = _find_config_file()

    raw_text = resolved.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)

    profiles_data: dict = data["profiles"]
    result: dict[str, ProfileConfig] = {}

    for name, cfg in profiles_data.items():
        resilience_raw: dict = cfg["resilience"]
        resilience = ResilienceConfig(
            retry_attempts=int(resilience_raw["retry_attempts"]),
            retry_delays=[int(d) for d in resilience_raw["retry_delays"]],
            fallback_model=resilience_raw.get("fallback_model") or None,
        )
        result[name] = ProfileConfig(
            model=str(cfg["model"]),
            temperature=float(cfg["temperature"]),
            max_tokens=int(cfg["max_tokens"]),
            resilience=resilience,
        )

    return result


def resolve_profile(
    profiles: dict[str, ProfileConfig],
    name: str,
) -> ProfileConfig:
    """Return the named profile, falling back to 'default' if not found.

    Args:
        profiles: Dictionary of loaded ProfileConfig instances keyed by name.
        name: The profile name to look up.

    Returns:
        The ProfileConfig for name, or the 'default' ProfileConfig if name
        is absent from the dictionary.

    Raises:
        KeyError: Neither the requested name nor 'default' exists in profiles.
    """
    if name in profiles:
        return profiles[name]
    return profiles["default"]
