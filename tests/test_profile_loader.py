"""Unit tests for common.profile_loader — written TDD (RED first)."""

import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from atelier.profile_loader import ProfileConfig, ResilienceConfig, load_profiles, resolve_profile
from atelier.agent_executor import _resolve_profile_model

# Path to the actual project default profiles file (used for integration-style tests
# that must verify the shipped configuration, not a fixture YAML).
_DEFAULT_PROFILES_PATH = Path(__file__).parent.parent / "config" / "atelier" / "profiles.yaml.default"


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
        base_url: null
        api_key_env: null
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      fast:
        model: mistral-small-2603
        temperature: 0.3
        max_tokens: 512
        base_url: null
        api_key_env: null
        resilience:
          retry_attempts: 2
          retry_delays: [1, 3]
      precise:
        model: claude-sonnet-4-5
        temperature: 0.2
        max_tokens: 4096
        base_url: null
        api_key_env: null
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
            base_url=None,
            api_key_env=None,
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
                base_url: null
                api_key_env: null
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
    """load_profiles() raises FileNotFoundError when resolve_config_path finds nothing.

    resolve_config_path (from common.config_loader) is monkeypatched to raise
    FileNotFoundError, verifying that load_profiles() propagates the error
    without swallowing it.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.profile_loader as _mod

    monkeypatch.setattr(
        _mod,
        "resolve_config_path",
        lambda _filename: (_ for _ in ()).throw(
            FileNotFoundError("atelier/profiles.yaml not found in config cascade")
        ),
    )

    with pytest.raises(FileNotFoundError, match="atelier/profiles.yaml not found"):
        load_profiles()


# ---------------------------------------------------------------------------
# 11b. load_profiles without config_path delegates to resolve_config_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_delegates_to_resolve_config_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """load_profiles() calls resolve_config_path('profiles.yaml') when no config_path given.

    After the fix, profile_loader must use resolve_config_path from
    common.config_loader rather than its own _find_config_file / _CASCADE_DIRS.
    This test monkeypatches common.profile_loader.resolve_config_path to return
    a controlled temp file, confirming the delegation without touching the real
    filesystem cascade.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
        tmp_path: Pytest-provided temporary directory.
    """
    import atelier.profile_loader as _mod

    controlled_yaml = tmp_path / "profiles.yaml"
    controlled_yaml.write_text(
        "profiles:\n"
        "  default:\n"
        "    model: cascade-delegated-model\n"
        "    temperature: 0.5\n"
        "    max_tokens: 512\n"
        "    base_url: null\n"
        "    api_key_env: null\n"
        "    resilience:\n"
        "      retry_attempts: 1\n"
        "      retry_delays: [1]\n"
    )

    calls: list[str] = []

    def _fake_resolve(filename: str) -> Path:
        calls.append(filename)
        return controlled_yaml

    monkeypatch.setattr(_mod, "resolve_config_path", _fake_resolve)

    profiles = load_profiles()

    assert calls == ["atelier/profiles.yaml"], (
        "load_profiles() must call resolve_config_path('atelier/profiles.yaml') exactly once"
    )
    assert profiles["default"].model == "cascade-delegated-model"


# ---------------------------------------------------------------------------
# 12. ProfileConfig.max_turns defaults to 20
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_max_turns_defaults_to_20(profiles_yaml: Path) -> None:
    """ProfileConfig.max_turns is 20 when not specified in the YAML profile.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].max_turns == 20
    assert profiles["fast"].max_turns == 20
    assert profiles["precise"].max_turns == 20


# ---------------------------------------------------------------------------
# 13. ProfileConfig.max_turns can be overridden in YAML
# ---------------------------------------------------------------------------


MAX_TURNS_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        max_turns: 10
        base_url: null
        api_key_env: null
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      capped:
        model: mistral-small-2603
        temperature: 0.5
        max_tokens: 1024
        max_turns: 5
        base_url: null
        api_key_env: null
        resilience:
          retry_attempts: 2
          retry_delays: [1, 3]
    """
)


@pytest.mark.unit
def test_profile_config_max_turns_overridden_from_yaml(tmp_path: Path) -> None:
    """ProfileConfig.max_turns reflects the value set in the YAML profile.

    When a profile defines max_turns explicitly, load_profiles() must store
    that value rather than the default of 20.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(MAX_TURNS_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["default"].max_turns == 10
    assert profiles["capped"].max_turns == 5


# ---------------------------------------------------------------------------
# 14. ProfileConfig.max_turns is included in direct construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_max_turns_constructable_directly() -> None:
    """ProfileConfig can be constructed with an explicit max_turns value.

    Verifies the field exists on the dataclass and accepts an integer directly,
    independent of the YAML loader.
    """
    profile = ProfileConfig(
        model="test-model",
        temperature=0.5,
        max_tokens=1024,
        max_turns=15,
        resilience=ResilienceConfig(retry_attempts=2, retry_delays=[1, 3]),
        base_url=None,
        api_key_env=None,
    )

    assert profile.max_turns == 15


# ---------------------------------------------------------------------------
# 15. ProfileConfig.fallback_model defaults to None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_fallback_model_defaults_to_none(profiles_yaml: Path) -> None:
    """ProfileConfig.fallback_model is None when not specified in YAML.

    This is distinct from ResilienceConfig.fallback_model — this lives directly
    on ProfileConfig for quick access without traversing the resilience sub-object.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].fallback_model is None
    assert profiles["fast"].fallback_model is None


# ---------------------------------------------------------------------------
# 25. ProfileConfig.fallback_model loaded from YAML when present
# ---------------------------------------------------------------------------


FALLBACK_MODEL_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        base_url: null
        api_key_env: null
        fallback_model: haiku-4-5
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      precise:
        model: claude-sonnet-4-5
        temperature: 0.3
        max_tokens: 4096
        base_url: null
        api_key_env: null
        fallback_model: mistral-small-2603
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_fallback_model_loaded_from_yaml(tmp_path: Path) -> None:
    """ProfileConfig.fallback_model is set to the string from YAML.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(FALLBACK_MODEL_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["default"].fallback_model == "haiku-4-5"
    assert profiles["precise"].fallback_model == "mistral-small-2603"


# ---------------------------------------------------------------------------
# 26. All new fields are accessible when constructing ProfileConfig directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_new_fields_constructable_directly() -> None:
    """ProfileConfig can be directly constructed with all fields.

    Verifies that fields exist on the dataclass, accept the correct types,
    and honour the frozen=True constraint.

    No YAML parsing involved — purely a dataclass construction test.
    """
    profile = ProfileConfig(
        model="test-model",
        temperature=0.5,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=2, retry_delays=[1, 3]),
        base_url="http://localhost:1234/v1",
        api_key_env="MY_API_KEY",
        fallback_model="haiku-4-5",
    )

    assert profile.base_url == "http://localhost:1234/v1"
    assert profile.api_key_env == "MY_API_KEY"
    assert profile.fallback_model == "haiku-4-5"


# ---------------------------------------------------------------------------
# 27. Backward compat: profiles without new fields load with defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_missing_required_fields_raises_key_error(tmp_path: Path) -> None:
    """load_profiles() raises KeyError when base_url or api_key_env is absent.

    Both base_url and api_key_env are required fields — every profile must
    declare them explicitly (even as null). Omitting them must raise KeyError,
    not silently fall back to a default.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    missing_fields_yaml = tmp_path / "profiles.yaml"
    missing_fields_yaml.write_text(
        textwrap.dedent(
            """\
            profiles:
              default:
                model: mistral-small-2603
                temperature: 0.7
                max_tokens: 2048
                resilience:
                  retry_attempts: 3
                  retry_delays: [2, 5, 15]
            """
        )
    )

    with pytest.raises(KeyError):
        load_profiles(config_path=missing_fields_yaml)


# ---------------------------------------------------------------------------
# 28. profiles.yaml.default includes the memory_extractor profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_includes_memory_extractor() -> None:
    """The shipped profiles.yaml.default must contain a 'memory_extractor' profile.

    This test reads the real default config file to ensure the profile was
    actually added — not just a fixture. Fails RED until the YAML is updated.
    """
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    assert "memory_extractor" in profiles, (
        "profiles.yaml.default is missing the 'memory_extractor' profile"
    )


# ---------------------------------------------------------------------------
# 29. memory_extractor profile uses anthropic:claude-haiku-4-5 (DeepAgents format)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_extractor_profile_model_is_haiku() -> None:
    """The memory_extractor profile must declare model='anthropic:claude-haiku-4-5'.

    Args: none (reads the shipped default config file).
    """
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    assert profiles["memory_extractor"].model == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# 30. memory_extractor profile has low temperature for deterministic JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_extractor_profile_temperature_is_low() -> None:
    """The memory_extractor profile must declare temperature=0.1 for deterministic output.

    Args: none (reads the shipped default config file).
    """
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    assert profiles["memory_extractor"].temperature == 0.1


# ---------------------------------------------------------------------------
# 31. All profiles in profiles.yaml.default use provider:model format (DeepAgents)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_profiles_model_format_uses_provider_prefix() -> None:
    """All models in profiles.yaml.default must use the 'provider:model' format.

    DeepAgents requires a provider prefix to route calls correctly without
    the LiteLLM proxy. Format: 'provider:model-name'
    (e.g., 'anthropic:claude-haiku-4-5').

    Args: none (reads the shipped default config file).
    """
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    for name, profile in profiles.items():
        assert ":" in profile.model, (
            f"Profile '{name}': model '{profile.model}' must use 'provider:model' format "
            f"(e.g., 'anthropic:claude-haiku-4-5'). LiteLLM proxy has been removed."
        )


# ---------------------------------------------------------------------------
# 32. base_url and api_key_env are loaded from YAML
# ---------------------------------------------------------------------------


BASE_URL_YAML = textwrap.dedent(
    """\
    profiles:
      local:
        model: openai:my-local-model
        temperature: 0.5
        max_tokens: 1024
        base_url: http://192.168.1.134:1234/v1
        api_key_env: null
        resilience:
          retry_attempts: 2
          retry_delays: [1, 3]
      cloud:
        model: anthropic:claude-haiku-4-5
        temperature: 0.7
        max_tokens: 512
        base_url: null
        api_key_env: MY_PROVIDER_KEY
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_base_url_and_api_key_env_loaded_from_yaml(tmp_path: Path) -> None:
    """ProfileConfig.base_url and api_key_env are read from YAML as-is.

    base_url may be a string URL or null; api_key_env may be an env var name or null.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(BASE_URL_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["local"].base_url == "http://192.168.1.134:1234/v1"
    assert profiles["local"].api_key_env is None
    assert profiles["cloud"].base_url is None
    assert profiles["cloud"].api_key_env == "MY_PROVIDER_KEY"


# ---------------------------------------------------------------------------
# 33. _resolve_profile_model — string passthrough when both fields are None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_returns_string_when_no_overrides() -> None:
    """_resolve_profile_model returns the model string when base_url and api_key_env are None.

    No init_chat_model call should be made in this path — create_deep_agent
    receives the raw provider:model string and resolves the provider itself.
    """
    profile = ProfileConfig(
        model="anthropic:claude-haiku-4-5",
        temperature=0.5,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env=None,
    )

    result = _resolve_profile_model(profile)

    assert result == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# 34. _resolve_profile_model — builds BaseChatModel when base_url is set
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_calls_init_chat_model_with_base_url(tmp_path: Path) -> None:
    """_resolve_profile_model calls init_chat_model with base_url when it is set.

    When base_url is provided (and api_key_env is None), init_chat_model must
    be called with only the base_url kwarg.
    """
    profile = ProfileConfig(
        model="openai:my-local-model",
        temperature=0.2,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url="http://localhost:1234/v1",
        api_key_env=None,
    )

    with patch("atelier.agent_executor.init_chat_model") as mock_init:
        _resolve_profile_model(profile)

    mock_init.assert_called_once_with("openai:my-local-model", base_url="http://localhost:1234/v1")


# ---------------------------------------------------------------------------
# 35. _resolve_profile_model — reads api_key from environment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_profile_model passes the env var value as api_key to init_chat_model.

    Args:
        monkeypatch: Pytest fixture for safe environment patching.
    """
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-test-secret")

    profile = ProfileConfig(
        model="anthropic:claude-haiku-4-5",
        temperature=0.7,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env="MY_PROVIDER_KEY",
    )

    with patch("atelier.agent_executor.init_chat_model") as mock_init:
        _resolve_profile_model(profile)

    mock_init.assert_called_once_with("anthropic:claude-haiku-4-5", api_key="sk-test-secret")


# ---------------------------------------------------------------------------
# 36. _resolve_profile_model — raises KeyError when api_key_env var is absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_profile_model_raises_key_error_for_missing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_profile_model raises KeyError when the env var named by api_key_env is absent.

    Fail-fast: missing credentials must never be silently swallowed.

    Args:
        monkeypatch: Pytest fixture for safe environment patching.
    """
    monkeypatch.delenv("ABSENT_KEY", raising=False)

    profile = ProfileConfig(
        model="anthropic:claude-haiku-4-5",
        temperature=0.7,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        base_url=None,
        api_key_env="ABSENT_KEY",
    )

    with pytest.raises(KeyError):
        _resolve_profile_model(profile)
