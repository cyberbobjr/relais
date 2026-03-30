"""Tests for aiguilleur.channel_config — ChannelConfig loading and parsing.

RED phase: these tests are written before any implementation exists.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from aiguilleur.channel_config import ChannelConfig, load_channels_config


# ---------------------------------------------------------------------------
# ChannelConfig dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_channel_config_is_frozen():
    """ChannelConfig must be immutable (frozen dataclass)."""
    cfg = ChannelConfig(name="discord")
    with pytest.raises((AttributeError, TypeError)):
        cfg.name = "telegram"  # type: ignore[misc]


@pytest.mark.unit
def test_channel_config_defaults():
    """ChannelConfig default values match spec."""
    cfg = ChannelConfig(name="discord")
    assert cfg.enabled is True
    assert cfg.streaming is False
    assert cfg.type == "native"
    assert cfg.command is None
    assert cfg.args == []
    assert cfg.class_path is None
    assert cfg.max_restarts == 5


# ---------------------------------------------------------------------------
# load_channels_config — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_channels_config_parses_enabled_and_streaming():
    """enabled and streaming flags are parsed correctly for each channel."""
    yaml_content = """
channels:
  discord:
    enabled: true
    streaming: true
  telegram:
    enabled: false
    streaming: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert configs["discord"].enabled is True
    assert configs["discord"].streaming is True
    assert configs["telegram"].enabled is False
    assert configs["telegram"].streaming is True


@pytest.mark.unit
def test_load_channels_config_name_field_matches_yaml_key():
    """ChannelConfig.name is set to the YAML key (e.g., 'discord')."""
    yaml_content = """
channels:
  discord:
    enabled: true
    streaming: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert configs["discord"].name == "discord"


@pytest.mark.unit
def test_load_channels_config_external_type_with_command_and_args():
    """External adapters have type='external', command, and args parsed."""
    yaml_content = """
channels:
  whatsapp:
    enabled: true
    streaming: false
    type: external
    command: node
    args:
      - adapters/whatsapp/index.js
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert configs["whatsapp"].type == "external"
    assert configs["whatsapp"].command == "node"
    assert configs["whatsapp"].args == ["adapters/whatsapp/index.js"]


@pytest.mark.unit
def test_load_channels_config_class_override():
    """A 'class' key in YAML is stored in class_path for dynamic loading."""
    yaml_content = """
channels:
  custom:
    enabled: true
    streaming: false
    class: mycompany.adapters.custom.CustomAiguilleur
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert configs["custom"].class_path == "mycompany.adapters.custom.CustomAiguilleur"


@pytest.mark.unit
def test_load_channels_config_max_restarts_override():
    """max_restarts can be set per-channel in YAML."""
    yaml_content = """
channels:
  discord:
    enabled: true
    streaming: true
    max_restarts: 10
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert configs["discord"].max_restarts == 10


# ---------------------------------------------------------------------------
# load_channels_config — fallback behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_channels_config_missing_file_returns_discord_fallback():
    """When channels.yaml is missing, fall back to discord enabled + streaming."""
    with patch(
        "aiguilleur.channel_config.resolve_config_path",
        side_effect=FileNotFoundError("not found"),
    ):
        configs = load_channels_config()

    assert "discord" in configs
    assert configs["discord"].enabled is True
    assert configs["discord"].streaming is True


@pytest.mark.unit
def test_load_channels_config_returns_dict_keyed_by_channel_name():
    """Return value is a dict[str, ChannelConfig] keyed by channel name."""
    yaml_content = """
channels:
  discord:
    enabled: true
    streaming: true
  telegram:
    enabled: false
    streaming: false
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    with patch("aiguilleur.channel_config.resolve_config_path", return_value=Path(tmp_path)):
        configs = load_channels_config()

    assert set(configs.keys()) == {"discord", "telegram"}
    assert all(isinstance(v, ChannelConfig) for v in configs.values())
