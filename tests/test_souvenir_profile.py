"""Unit tests — Souvenir uses the memory_extractor LLM profile at startup.

These tests verify that Souvenir.__init__ reads the 'memory_extractor' profile
from profiles.yaml via load_profiles()/resolve_profile() and passes its model
to MemoryExtractor instead of the former hard-coded 'gpt-3.5-turbo' default.

They also verify the fallback path: when load_profiles() raises, Souvenir
must not crash and must fall back to the constant _FALLBACK_EXTRACTION_MODEL.
"""

from unittest.mock import MagicMock, patch

import pytest

from atelier.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(model: str = "test-model") -> ProfileConfig:
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
    )


# ---------------------------------------------------------------------------
# 1. Souvenir uses the model from the memory_extractor profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_uses_memory_extractor_profile_model() -> None:
    """Souvenir.__init__ must pass the memory_extractor profile's model to MemoryExtractor.

    When load_profiles() returns a dict containing a 'memory_extractor' profile
    with model='test-model', Souvenir._extractor._model must equal 'test-model'.
    """
    test_profile = _make_profile(model="test-model")
    mock_profiles = {"memory_extractor": test_profile}

    with (
        patch("souvenir.main.load_profiles", return_value=mock_profiles),
        patch("souvenir.main.RedisClient"),
        patch("souvenir.main.LongTermStore"),
    ):
        from souvenir.main import Souvenir

        souvenir = Souvenir()

    assert souvenir._extractor._model == "test-model"


# ---------------------------------------------------------------------------
# 2. Souvenir falls back to glm-4.7-flash when profile loading fails
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_souvenir_falls_back_to_glm_on_profile_load_failure() -> None:
    """Souvenir.__init__ must not crash when load_profiles() raises FileNotFoundError.

    The fallback model must be the _FALLBACK_EXTRACTION_MODEL constant
    ('glm-4.7-flash'). A WARNING must be logged but no exception is raised.
    """
    with (
        patch("souvenir.main.load_profiles", side_effect=FileNotFoundError("no file")),
        patch("souvenir.main.RedisClient"),
        patch("souvenir.main.LongTermStore"),
    ):
        from souvenir.main import Souvenir, _FALLBACK_EXTRACTION_MODEL

        souvenir = Souvenir()

    assert souvenir._extractor._model == _FALLBACK_EXTRACTION_MODEL
    assert _FALLBACK_EXTRACTION_MODEL == "glm-4.7-flash"
