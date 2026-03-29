"""Unit tests for atelier.profile_loader — written TDD (RED first)."""

import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from atelier.profile_loader import ProfileConfig, ResilienceConfig, load_profiles, resolve_profile

# Path to the actual project default profiles file (used for integration-style tests
# that must verify the shipped configuration, not a fixture YAML).
_DEFAULT_PROFILES_PATH = Path(__file__).parent.parent / "config" / "profiles.yaml.default"


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
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      capped:
        model: mistral-small-2603
        temperature: 0.5
        max_tokens: 1024
        max_turns: 5
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
    )

    assert profile.max_turns == 15


# ---------------------------------------------------------------------------
# 15. ProfileConfig.allowed_tools defaults to None (unrestricted)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_allowed_tools_defaults_to_none(profiles_yaml: Path) -> None:
    """ProfileConfig.allowed_tools is None when not specified in YAML.

    None means unrestricted — the executor should not filter any tool.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].allowed_tools is None
    assert profiles["fast"].allowed_tools is None


# ---------------------------------------------------------------------------
# 16. ProfileConfig.allowed_tools loaded as tuple when present
# ---------------------------------------------------------------------------


ALLOWED_TOOLS_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      coder:
        model: qwen3-coder-30b
        temperature: 0.2
        max_tokens: 8192
        allowed_tools: [Read, "Bash(git *)", "mcp__jcodemunch__*"]
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_allowed_tools_loaded_as_tuple(tmp_path: Path) -> None:
    """ProfileConfig.allowed_tools is a tuple of strings when specified in YAML.

    The field must be a tuple (not a list) to satisfy the frozen=True constraint
    and immutability requirement.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(ALLOWED_TOOLS_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["coder"].allowed_tools == ("Read", "Bash(git *)", "mcp__jcodemunch__*")
    assert isinstance(profiles["coder"].allowed_tools, tuple)


# ---------------------------------------------------------------------------
# 17. ProfileConfig.allowed_mcp defaults to None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_allowed_mcp_defaults_to_none(profiles_yaml: Path) -> None:
    """ProfileConfig.allowed_mcp is None when not specified in YAML.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].allowed_mcp is None


# ---------------------------------------------------------------------------
# 18. ProfileConfig.allowed_mcp loaded as tuple when present
# ---------------------------------------------------------------------------


ALLOWED_MCP_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        allowed_mcp: ["mcp__gitlab__*", "mcp__brave__search"]
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_allowed_mcp_loaded_as_tuple(tmp_path: Path) -> None:
    """ProfileConfig.allowed_mcp is a tuple of strings when specified in YAML.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(ALLOWED_MCP_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["default"].allowed_mcp == ("mcp__gitlab__*", "mcp__brave__search")
    assert isinstance(profiles["default"].allowed_mcp, tuple)


# ---------------------------------------------------------------------------
# 19. ProfileConfig.guardrails defaults to empty tuple
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_guardrails_defaults_to_empty_tuple(profiles_yaml: Path) -> None:
    """ProfileConfig.guardrails is an empty tuple when not specified in YAML.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].guardrails == ()
    assert isinstance(profiles["default"].guardrails, tuple)


# ---------------------------------------------------------------------------
# 20. ProfileConfig.guardrails loaded as tuple when present
# ---------------------------------------------------------------------------


GUARDRAILS_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        guardrails: [no_bash, no_code_exec]
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_guardrails_loaded_as_tuple(tmp_path: Path) -> None:
    """ProfileConfig.guardrails is a tuple of strings when specified in YAML.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(GUARDRAILS_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["default"].guardrails == ("no_bash", "no_code_exec")
    assert isinstance(profiles["default"].guardrails, tuple)


# ---------------------------------------------------------------------------
# 21. ProfileConfig.memory_scope defaults to "own"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_memory_scope_defaults_to_own(profiles_yaml: Path) -> None:
    """ProfileConfig.memory_scope is "own" when not specified in YAML.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    assert profiles["default"].memory_scope == "own"
    assert profiles["fast"].memory_scope == "own"


# ---------------------------------------------------------------------------
# 22. ProfileConfig.memory_scope accepts valid scope values from YAML
# ---------------------------------------------------------------------------


MEMORY_SCOPE_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        memory_scope: global
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      task_profile:
        model: mistral-small-2603
        temperature: 0.5
        max_tokens: 1024
        memory_scope: task
        resilience:
          retry_attempts: 2
          retry_delays: [1, 3]
    """
)


@pytest.mark.unit
def test_profile_config_memory_scope_loaded_from_yaml(tmp_path: Path) -> None:
    """ProfileConfig.memory_scope reflects the value in YAML.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(MEMORY_SCOPE_YAML)

    profiles = load_profiles(config_path=p)

    assert profiles["default"].memory_scope == "global"
    assert profiles["task_profile"].memory_scope == "task"


# ---------------------------------------------------------------------------
# 23. ProfileConfig.memory_scope raises ValueError on invalid scope
# ---------------------------------------------------------------------------


INVALID_MEMORY_SCOPE_YAML = textwrap.dedent(
    """\
    profiles:
      default:
        model: mistral-small-2603
        temperature: 0.7
        max_tokens: 2048
        memory_scope: invalid_scope
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
    """
)


@pytest.mark.unit
def test_profile_config_memory_scope_invalid_raises_value_error(tmp_path: Path) -> None:
    """load_profiles() raises ValueError when memory_scope is not a recognised value.

    Valid scopes are: "global", "own", "sender", "task".

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    p = tmp_path / "profiles.yaml"
    p.write_text(INVALID_MEMORY_SCOPE_YAML)

    with pytest.raises(ValueError, match="memory_scope"):
        load_profiles(config_path=p)


# ---------------------------------------------------------------------------
# 24. ProfileConfig.fallback_model defaults to None
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
        fallback_model: haiku-4-5
        resilience:
          retry_attempts: 3
          retry_delays: [2, 5, 15]
      precise:
        model: claude-sonnet-4-5
        temperature: 0.3
        max_tokens: 4096
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
    """ProfileConfig can be directly constructed with all new Wave-1A fields.

    Verifies that all five new fields exist on the dataclass, accept the
    correct types, and honour the frozen=True constraint.

    No YAML parsing involved — purely a dataclass construction test.
    """
    profile = ProfileConfig(
        model="test-model",
        temperature=0.5,
        max_tokens=1024,
        resilience=ResilienceConfig(retry_attempts=2, retry_delays=[1, 3]),
        allowed_tools=("Read", "Write"),
        allowed_mcp=("mcp__brave__search",),
        guardrails=("no_bash",),
        memory_scope="sender",
        fallback_model="haiku-4-5",
    )

    assert profile.allowed_tools == ("Read", "Write")
    assert profile.allowed_mcp == ("mcp__brave__search",)
    assert profile.guardrails == ("no_bash",)
    assert profile.memory_scope == "sender"
    assert profile.fallback_model == "haiku-4-5"


# ---------------------------------------------------------------------------
# 27. Backward compat: profiles without new fields load with defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profile_config_backward_compat_missing_new_fields(profiles_yaml: Path) -> None:
    """Profiles without any Wave-1A fields still load successfully with defaults.

    The MINIMAL_YAML fixture has no allowed_tools, allowed_mcp, guardrails,
    memory_scope, or fallback_model.  load_profiles() must apply defaults
    rather than raising.

    Args:
        profiles_yaml: Fixture path to the temporary profiles YAML file.
    """
    profiles = load_profiles(config_path=profiles_yaml)

    for name in ("default", "fast", "precise"):
        p = profiles[name]
        assert p.allowed_tools is None, f"{name}: allowed_tools should be None"
        assert p.allowed_mcp is None, f"{name}: allowed_mcp should be None"
        assert p.guardrails == (), f"{name}: guardrails should be ()"
        assert p.memory_scope == "own", f"{name}: memory_scope should be 'own'"
        # fallback_model may be None or a string depending on the profile
        assert isinstance(p.fallback_model, (str, type(None))), (
            f"{name}: fallback_model must be str or None"
        )


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
# 29. memory_extractor profile uses glm-4.7-flash
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_memory_extractor_profile_model_is_glm() -> None:
    """The memory_extractor profile must declare model='glm-4.7-flash'.

    Args: none (reads the shipped default config file).
    """
    profiles = load_profiles(config_path=_DEFAULT_PROFILES_PATH)

    assert profiles["memory_extractor"].model == "glm-4.7-flash"


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
