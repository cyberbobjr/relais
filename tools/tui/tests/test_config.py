"""Tests for relais_tui.config — TDD RED phase."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from relais_tui.config import Config, ThemeConfig, _default_config_path, load_config, save_config

class TestThemeConfig:
    """ThemeConfig frozen dataclass tests."""

    def test_default_values(self) -> None:
        theme = ThemeConfig()
        assert theme.background == "#1a1a2e"
        assert theme.user_text == "#8be9fd"
        assert theme.assistant_text == "#f8f8f2"
        assert theme.code_block == "#282a36"
        assert theme.progress == "#6272a4"
        assert theme.error == "#ff5555"
        assert theme.metadata == "#6272a4"
        assert theme.status_bar == "#16213e"
        assert theme.accent == "#50fa7b"

    def test_frozen(self) -> None:
        theme = ThemeConfig()
        with pytest.raises(FrozenInstanceError):
            theme.background = "#000000"  # type: ignore[misc]

    def test_custom_values(self) -> None:
        theme = ThemeConfig(background="#000000", error="#ff0000")
        assert theme.background == "#000000"
        assert theme.error == "#ff0000"
        # Other fields keep defaults
        assert theme.accent == "#50fa7b"


class TestConfig:
    """Config frozen dataclass tests."""

    def test_default_values(self) -> None:
        cfg = Config()
        assert cfg.api_url == "http://localhost:8080"
        assert cfg.api_key == ""
        assert cfg.history_path == "~/.relais/storage/tui/history"
        assert cfg.request_timeout == 120
        assert isinstance(cfg.theme, ThemeConfig)

    def test_frozen(self) -> None:
        cfg = Config()
        with pytest.raises(FrozenInstanceError):
            cfg.api_url = "http://other"  # type: ignore[misc]

    def test_custom_values(self) -> None:
        cfg = Config(api_url="http://custom:9090", request_timeout=60)
        assert cfg.api_url == "http://custom:9090"
        assert cfg.request_timeout == 60


# ---------------------------------------------------------------------------
# _default_config_path
# ---------------------------------------------------------------------------


class TestDefaultConfigPath:
    """Tests for RELAIS_HOME-based default path resolution."""

    def test_uses_relais_home_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RELAIS_HOME", "/opt/relais")
        path = _default_config_path()
        assert path == Path("/opt/relais/config/tui/config.yaml")

    def test_falls_back_to_home_relais(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RELAIS_HOME", raising=False)
        path = _default_config_path()
        assert path == Path("~/.relais/config/tui/config.yaml")

    def test_relais_home_empty_uses_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RELAIS_HOME", "")
        path = _default_config_path()
        assert path == Path("~/.relais/config/tui/config.yaml")

    def test_load_config_respects_relais_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        relais_home = tmp_path / "custom_relais"
        monkeypatch.setenv("RELAIS_HOME", str(relais_home))
        cfg = load_config()  # no explicit path — should use RELAIS_HOME
        expected = relais_home / "config" / "tui" / "config.yaml"
        assert expected.exists()
        assert cfg.api_url == "http://localhost:8080"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config()."""

    def test_creates_default_file_on_first_run(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        assert not config_path.exists()

        cfg = load_config(config_path)

        assert config_path.exists()
        assert cfg.api_url == "http://localhost:8080"
        assert cfg.api_key == ""

    def test_file_permissions_0600(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        load_config(config_path)

        mode = config_path.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.yaml"
        load_config(config_path)
        assert config_path.exists()

    def test_loads_existing_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        data = {
            "api_url": "http://myserver:3000",
            "api_key": "sk-test-123",
            "request_timeout": 60,
        }
        config_path.write_text(yaml.dump(data))

        cfg = load_config(config_path)

        assert cfg.api_url == "http://myserver:3000"
        assert cfg.api_key == "sk-test-123"
        assert cfg.request_timeout == 60

    def test_missing_keys_get_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"api_url": "http://custom"}))

        cfg = load_config(config_path)

        assert cfg.api_url == "http://custom"
        assert cfg.api_key == ""  # default
        assert cfg.request_timeout == 120  # default

    def test_partial_theme_gets_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        data = {"theme": {"background": "#000000"}}
        config_path.write_text(yaml.dump(data))

        cfg = load_config(config_path)

        assert cfg.theme.background == "#000000"
        assert cfg.theme.accent == "#50fa7b"  # default

    def test_env_var_overrides_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "config.yaml"
        data = {"api_key": "from-file"}
        config_path.write_text(yaml.dump(data))
        monkeypatch.setenv("RELAIS_TUI_API_KEY", "from-env")

        cfg = load_config(config_path)

        assert cfg.api_key == "from-env"

    def test_env_var_empty_does_not_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "config.yaml"
        data = {"api_key": "from-file"}
        config_path.write_text(yaml.dump(data))
        monkeypatch.setenv("RELAIS_TUI_API_KEY", "")

        cfg = load_config(config_path)

        assert cfg.api_key == "from-file"

    def test_env_var_unset_uses_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "config.yaml"
        data = {"api_key": "from-file"}
        config_path.write_text(yaml.dump(data))
        monkeypatch.delenv("RELAIS_TUI_API_KEY", raising=False)

        cfg = load_config(config_path)

        assert cfg.api_key == "from-file"

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("")

        cfg = load_config(config_path)

        assert cfg == Config()

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        data = {"api_url": "http://x", "unknown_field": "whatever"}
        config_path.write_text(yaml.dump(data))

        cfg = load_config(config_path)

        assert cfg.api_url == "http://x"
        assert not hasattr(cfg, "unknown_field")


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------


class TestSaveConfig:
    """Tests for save_config()."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        original = Config(api_url="http://test:1234", api_key="secret")

        save_config(original, config_path)
        loaded = load_config(config_path)

        assert loaded.api_url == original.api_url
        assert loaded.api_key == original.api_key
        assert loaded.theme == original.theme

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        config_path = tmp_path / "a" / "b" / "config.yaml"
        save_config(Config(), config_path)
        assert config_path.exists()

    def test_save_sets_permissions(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        save_config(Config(), config_path)

        mode = config_path.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_saved_yaml_is_readable(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        save_config(Config(), config_path)

        raw = yaml.safe_load(config_path.read_text())
        assert isinstance(raw, dict)
        assert raw["api_url"] == "http://localhost:8080"
        assert "theme" in raw
        assert raw["theme"]["background"] == "#1a1a2e"
