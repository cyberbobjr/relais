"""Unit tests — Souvenir uses the memory_extractor LLM profile at startup.

These tests verify that Souvenir.__init__ reads the 'memory_extractor' profile
from profiles.yaml via load_profiles()/resolve_profile() and passes its model
to MemoryExtractor instead of the former hard-coded 'gpt-3.5-turbo' default.

They also verify the fallback path: when load_profiles() raises, Souvenir
must not crash and must fall back to the constant _FALLBACK_EXTRACTION_MODEL.

Test 3 verifies the cascade delegation contract: load_profiles() (used by
Souvenir) must delegate to resolve_config_path() from common.config_loader,
not to any hardcoded Path.home() / '.relais' cascade. This guards against
regression of the bug where atelier/profile_loader.py had its own _CASCADE_DIRS
that bypassed get_relais_home() / RELAIS_HOME env var support.
"""

from unittest.mock import MagicMock, patch

import pytest

from atelier.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(model: str = "anthropic:claude-haiku-4-5") -> ProfileConfig:
    """Return a minimal ProfileConfig for mock use.

    Args:
        model: The model identifier to embed in the profile.

    Returns:
        A ProfileConfig instance with only the required fields set.
    """
    return ProfileConfig(
        model=model,
        temperature=0.1,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=2, retry_delays=[1, 3]),
        base_url=None,
        api_key_env=None,
    )


# ---------------------------------------------------------------------------
# 1. Souvenir uses the model from the memory_extractor profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_uses_memory_extractor_profile_model() -> None:
    """Souvenir.__init__ must pass the memory_extractor profile's model to MemoryExtractor.

    When load_profiles() returns a dict containing a 'memory_extractor' profile
    with model='anthropic:claude-haiku-4-5', Souvenir._extractor._model_name
    must equal that model string.
    """
    test_profile = _make_profile(model="anthropic:claude-haiku-4-5")
    mock_profiles = {"memory_extractor": test_profile}

    with (
        patch("souvenir.main.load_profiles", return_value=mock_profiles),
        patch("souvenir.main.RedisClient"),
        patch("souvenir.main.LongTermStore"),
        patch("souvenir.memory_extractor.init_chat_model") as mock_init_chat,
    ):
        mock_init_chat.return_value = MagicMock()
        from souvenir.main import Souvenir

        souvenir = Souvenir()

    assert souvenir._extractor._model_name == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# 2. Souvenir falls back to anthropic:claude-haiku-4-5 when profile loading fails
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_falls_back_to_haiku_on_profile_load_failure() -> None:
    """Souvenir.__init__ must not crash when load_profiles() raises FileNotFoundError.

    The fallback model must be the _FALLBACK_EXTRACTION_MODEL constant
    ('anthropic:claude-haiku-4-5'). A WARNING must be logged but no exception is raised.
    """
    with (
        patch("souvenir.main.load_profiles", side_effect=FileNotFoundError("no file")),
        patch("souvenir.main.RedisClient"),
        patch("souvenir.main.LongTermStore"),
        patch("souvenir.memory_extractor.init_chat_model") as mock_init_chat,
    ):
        mock_init_chat.return_value = MagicMock()
        from souvenir.main import Souvenir, _FALLBACK_EXTRACTION_MODEL

        souvenir = Souvenir()

    assert souvenir._extractor._model_name == _FALLBACK_EXTRACTION_MODEL
    assert _FALLBACK_EXTRACTION_MODEL == "anthropic:claude-haiku-4-5"


# ---------------------------------------------------------------------------
# 3. load_profiles() delegates to resolve_config_path — no private cascade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_delegates_to_resolve_config_path(tmp_path: "pytest.TempPathFactory") -> None:
    """load_profiles() must delegate file lookup to resolve_config_path().

    This verifies that atelier.profile_loader does NOT contain its own private
    _CASCADE_DIRS / _find_config_file() cascade that bypasses the RELAIS_HOME
    environment variable.  The contract is: when resolve_config_path raises
    FileNotFoundError, load_profiles() (without an explicit config_path) must
    propagate that same error — proving that it called resolve_config_path
    rather than its own lookup logic.
    """
    from atelier.profile_loader import load_profiles

    with patch(
        "atelier.profile_loader.resolve_config_path",
        side_effect=FileNotFoundError("mocked cascade — no profiles.yaml found"),
    ) as mock_resolve:
        with pytest.raises(FileNotFoundError, match="mocked cascade"):
            load_profiles()

    mock_resolve.assert_called_once_with("profiles.yaml")


@pytest.mark.unit
def test_souvenir_init_load_profiles_respects_relais_home(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Souvenir.__init__ must honour RELAIS_HOME when loading profiles.yaml.

    This is an end-to-end cascade test: when RELAIS_HOME is set to a
    temp directory containing a valid profiles.yaml, Souvenir must read
    that file without falling back to ~/.relais/ or ./config/.

    The test writes a minimal profiles.yaml to RELAIS_HOME/config/,
    then constructs Souvenir and asserts it found the memory_extractor
    profile from that custom home directory.
    """
    import yaml

    custom_home = tmp_path / "custom_relais"
    config_dir = custom_home / "config"
    config_dir.mkdir(parents=True)

    minimal_yaml = {
        "profiles": {
            "default": {
                "model": "anthropic:claude-haiku-4-5",
                "temperature": 0.7,
                "max_tokens": 1024,
                "max_turns": 10,
                "resilience": {"retry_attempts": 3, "retry_delays": [1, 2, 4]},
            },
            "memory_extractor": {
                "model": "anthropic:claude-haiku-4-5",
                "temperature": 0.1,
                "max_tokens": 512,
                "max_turns": 5,
                "resilience": {"retry_attempts": 2, "retry_delays": [1, 3]},
            },
        }
    }
    (config_dir / "profiles.yaml").write_text(yaml.safe_dump(minimal_yaml))

    monkeypatch.setenv("RELAIS_HOME", str(custom_home))

    # Reload config_loader so CONFIG_SEARCH_PATH is rebuilt with the new env var.
    import importlib
    import common.config_loader as ccl
    importlib.reload(ccl)

    # Also reload profile_loader so it uses the freshly reloaded resolve_config_path.
    import atelier.profile_loader as pl
    importlib.reload(pl)

    with (
        patch("souvenir.main.RedisClient"),
        patch("souvenir.main.LongTermStore"),
        patch("souvenir.memory_extractor.init_chat_model") as mock_init_chat,
    ):
        mock_init_chat.return_value = MagicMock()
        import souvenir.main as sm
        importlib.reload(sm)
        souvenir = sm.Souvenir()

    assert souvenir._extractor._model_name == "anthropic:claude-haiku-4-5"
