"""Tests for common.config_loader.get_default_llm_profile.

RED phase: written before implementation — all tests fail until
get_default_llm_profile() is added to common/config_loader.py.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from common.config_loader import get_default_llm_profile


# ---------------------------------------------------------------------------
# get_default_llm_profile — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_default_llm_profile_returns_value_from_config_yaml():
    """Returns llm.default_profile from config.yaml when present."""
    yaml_content = """
llm:
  default_profile: precise
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = Path(f.name)

    with patch("common.config_loader.resolve_config_path", return_value=tmp_path):
        result = get_default_llm_profile()

    assert result == "precise"


@pytest.mark.unit
def test_get_default_llm_profile_returns_default_when_key_absent():
    """Returns 'default' when llm.default_profile is not in config.yaml."""
    yaml_content = """
redis:
  socket_path: /tmp/redis.sock
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = Path(f.name)

    with patch("common.config_loader.resolve_config_path", return_value=tmp_path):
        result = get_default_llm_profile()

    assert result == "default"


@pytest.mark.unit
def test_get_default_llm_profile_returns_default_when_llm_section_absent():
    """Returns 'default' when the entire 'llm' section is absent from config.yaml."""
    yaml_content = """
logging:
  level: INFO
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = Path(f.name)

    with patch("common.config_loader.resolve_config_path", return_value=tmp_path):
        result = get_default_llm_profile()

    assert result == "default"


@pytest.mark.unit
def test_get_default_llm_profile_returns_default_when_file_not_found():
    """Returns 'default' when config.yaml is not found (FileNotFoundError)."""
    with patch(
        "common.config_loader.resolve_config_path",
        side_effect=FileNotFoundError("not found"),
    ):
        result = get_default_llm_profile()

    assert result == "default"


@pytest.mark.unit
def test_get_default_llm_profile_returns_default_when_config_empty():
    """Returns 'default' when config.yaml is empty."""
    yaml_content = ""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = Path(f.name)

    with patch("common.config_loader.resolve_config_path", return_value=tmp_path):
        result = get_default_llm_profile()

    assert result == "default"


@pytest.mark.unit
def test_get_default_llm_profile_uses_fast_profile():
    """Returns 'fast' when llm.default_profile is set to 'fast'."""
    yaml_content = """
llm:
  default_profile: fast
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = Path(f.name)

    with patch("common.config_loader.resolve_config_path", return_value=tmp_path):
        result = get_default_llm_profile()

    assert result == "fast"
