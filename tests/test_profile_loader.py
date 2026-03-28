"""Unit tests for atelier.profile_loader — written TDD (RED first)."""

import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from atelier.profile_loader import ProfileConfig, ResilienceConfig, load_profiles, resolve_profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      fast:
        model: mistral-small-2603
        temperature: 0.3
        max_tokens: 512
        resilience:
          retry_attempts: 2
          retry_delays: [1, 3]
      precise:
        model: claude-sonnet-4-5
        temperature: 0.2
        max_tokens: 4096
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
          fallback_model: mistral-small-2603
    """
)


@pytest.fixture()
def profiles_yaml(tmp_path: Path) -> Path:
    """Write the minimal YAML fixture to a temporary file and return its path.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written YAML file.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(MINIMAL_YAML)
    return p


# ---------------------------------------------------------------------------
# 1. load_profiles returns all profiles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_returns_all_profiles(profiles_yaml: Path) -> None:
    """load_profiles() returns a dict containing every profile defined in the YAML.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert set(profiles.keys()) == {"default", "fast", "precise"}
    assert isinstance(profiles["default"], ProfileConfig)
    assert isinstance(profiles["fast"], ProfileConfig)
    assert isinstance(profiles["precise"], ProfileConfig)


# ---------------------------------------------------------------------------
# 2. ProfileConfig is frozen (immutable)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_is_frozen(profiles_yaml: Path) -> None:
    """ProfileConfig instances are immutable; attribute assignment raises FrozenInstanceError.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)
    profile = profiles["default"]

    with pytest.raises(FrozenInstanceError):
        profile.model = "new-model"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. resolve_profile — known profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_known(profiles_yaml: Path) -> None:
    """resolve_profile() returns the matching ProfileConfig when name exists.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)
    result = resolve_profile(profiles, "fast")

    assert result.model == "mistral-small-2603"
    assert result.temperature == 0.3
    assert result.max_tokens == 512


# ---------------------------------------------------------------------------
# 4. resolve_profile — unknown name falls back to "default"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_unknown_falls_back_to_default(profiles_yaml: Path) -> None:
    """resolve_profile() returns the default profile when the requested name is absent.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)
    result = resolve_profile(profiles, "nonexistent")

    default = profiles["default"]
    assert result == default


# ---------------------------------------------------------------------------
# 5. resolve_profile — missing "default" raises KeyError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_missing_default_raises() -> None:
    """resolve_profile() raises KeyError when neither the name nor 'default' exists.

    When the profiles dict has no 'default' key and the requested name is unknown,
    a KeyError must be raised.
    """
    profiles: dict[str, ProfileConfig] = {
        "fast": ProfileConfig(
            model="mistral-small-2603",
            temperature=0.3,
            max_tokens=512,
            resilience=ResilienceConfig(retry_attempts=2, retry_delays=[1, 3]),
        )
    }

    with pytest.raises(KeyError):
        resolve_profile(profiles, "nonexistent")


# ---------------------------------------------------------------------------
# 6. ResilienceConfig — retry_delays loaded as list[int]
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resilience_config_has_correct_delays(profiles_yaml: Path) -> None:
    """retry_delays is loaded as a list of integers matching the YAML values.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)
    resilience = profiles["default"].resilience

    assert isinstance(resilience, ResilienceConfig)
    assert resilience.retry_delays == [2, 5, 15]
    assert all(isinstance(d, int) for d in resilience.retry_delays)


# ---------------------------------------------------------------------------
# 7. load_profiles with explicit path — no filesystem side effects
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_with_explicit_path(tmp_path: Path) -> None:
    """load_profiles() reads the given path directly without touching the config cascade.

    Writing and reading an isolated file confirms no ~/.relais or /opt/relais I/O
    occurs when config_path is provided.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    isolated_yaml = tmp_path / "isolated_profiles.yaml"
    isolated_yaml.write_text(
        textwrap.dedent(
            """\
            profiles:
              default:
                model: test-model-only
                temperature: 0.1
                max_tokens: 100
                resilience:
                  retry_attempts: 1
                  retry_delays: [1]
            """
        )
    )

    profiles = load_profiles(config_path=isolated_yaml)

    assert list(profiles.keys()) == ["default"]
    assert profiles["default"].model == "test-model-only"


# ---------------------------------------------------------------------------
# 8. fallback_model is None by default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resilience_fallback_model_defaults_to_none(profiles_yaml: Path) -> None:
    """ResilienceConfig.fallback_model is None when not specified in YAML.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["fast"].resilience.fallback_model is None


# ---------------------------------------------------------------------------
# 9. fallback_model is set when present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resilience_fallback_model_set_when_present(profiles_yaml: Path) -> None:
    """ResilienceConfig.fallback_model is populated when the YAML specifies one.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["precise"].resilience.fallback_model == "mistral-small-2603"


# ---------------------------------------------------------------------------
# 10. ResilienceConfig is also frozen
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resilience_config_is_frozen(profiles_yaml: Path) -> None:
    """ResilienceConfig instances are immutable; attribute mutation raises FrozenInstanceError.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)
    resilience = profiles["default"].resilience

    with pytest.raises(FrozenInstanceError):
        resilience.retry_attempts = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 11. load_profiles without config_path raises FileNotFoundError when cascade empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_raises_when_cascade_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_profiles() raises FileNotFoundError when no profiles.yaml exists in the cascade.

    The cascade directories are monkeypatched to empty tmp paths that contain no
    profiles.yaml so the FileNotFoundError branch is exercised without touching
    real filesystem locations.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.profile_loader as _mod

    monkeypatch.setattr(_mod, "_CASCADE_DIRS", [Path("/nonexistent/__cascade_test__")])

    with pytest.raises(FileNotFoundError, match="profiles.yaml not found"):
        load_profiles()
