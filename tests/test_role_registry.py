"""Unit tests for common.role_registry — RED phase (TDD).

These tests define the expected contract for RoleRegistry BEFORE implementation.
Run: pytest tests/test_role_registry.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from common.role_registry import RoleConfig, RoleRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "users.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# RoleConfig contract
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRoleConfig:
    def test_is_frozen_dataclass(self) -> None:
        """RoleConfig must be immutable."""
        cfg = RoleConfig(
            actions=("send",),
            skills_dirs=("tools",),
            allowed_mcp_tools=("*",),
        )
        with pytest.raises((AttributeError, TypeError)):
            cfg.actions = ("other",)  # type: ignore[misc]

    def test_default_empty_tuples(self) -> None:
        """All tuple fields must default to empty tuple."""
        cfg = RoleConfig()
        assert cfg.actions == ()
        assert cfg.skills_dirs == ()
        assert cfg.allowed_mcp_tools == ()


# ---------------------------------------------------------------------------
# RoleRegistry — basic loading
# ---------------------------------------------------------------------------

_FULL_YAML = """\
roles:
  admin:
    actions: ["send", "command", "admin"]
    skills_dirs: ["*"]
    allowed_mcp_tools: ["*"]
  user:
    actions: ["send"]
    skills_dirs: ["general"]
    allowed_mcp_tools: ["search__web"]
  guest:
    actions: []
    skills_dirs: []
    allowed_mcp_tools: []
"""

@pytest.mark.unit
class TestRoleRegistryLoad:
    def test_get_role_admin(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _FULL_YAML)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("admin")
        assert role is not None
        assert role.actions == ("send", "command", "admin")
        assert role.skills_dirs == ("*",)
        assert role.allowed_mcp_tools == ("*",)

    def test_get_role_user(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _FULL_YAML)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("user")
        assert role is not None
        assert role.actions == ("send",)
        assert role.skills_dirs == ("general",)
        assert role.allowed_mcp_tools == ("search__web",)

    def test_get_unknown_role_returns_none(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _FULL_YAML)
        registry = RoleRegistry(config_path=p)
        assert registry.get_role("superadmin") is None

    def test_permissive_mode_missing_file(self) -> None:
        """When users.yaml is not found, RoleRegistry must not raise and must
        return None for all lookups."""
        registry = RoleRegistry(config_path=Path("/nonexistent/users.yaml"))
        assert registry.get_role("admin") is None

    def test_permissive_mode_no_config_path(self, tmp_path: Path) -> None:
        """When no config_path is provided and no users.yaml exists in the
        cascade, RoleRegistry must not raise."""
        # We cannot control the cascade here, so we just ensure no exception
        # is raised even if the file happens not to exist.
        try:
            registry = RoleRegistry(config_path=Path(tmp_path / "missing.yaml"))
            registry.get_role("admin")  # must not raise
        except Exception as exc:
            pytest.fail(f"RoleRegistry raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Normalisation rules
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNormalisation:
    def _registry_for(self, tmp_path: Path, roles_yaml: str) -> RoleRegistry:
        content = f"roles:\n  test:\n{roles_yaml}"
        p = _write_yaml(tmp_path, content)
        return RoleRegistry(config_path=p)

    def test_null_skills_dirs_normalizes_to_empty(self, tmp_path: Path) -> None:
        reg = self._registry_for(
            tmp_path,
            "    actions: []\n    skills_dirs: null\n    allowed_mcp_tools: []\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.skills_dirs == ()

    def test_empty_list_skills_dirs_normalizes_to_empty(self, tmp_path: Path) -> None:
        reg = self._registry_for(
            tmp_path,
            "    actions: []\n    skills_dirs: []\n    allowed_mcp_tools: []\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.skills_dirs == ()

    def test_wildcard_list_normalizes_to_tuple(self, tmp_path: Path) -> None:
        reg = self._registry_for(
            tmp_path,
            "    actions: []\n    skills_dirs: [\"*\"]\n    allowed_mcp_tools: [\"*\"]\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.skills_dirs == ("*",)
        assert role.allowed_mcp_tools == ("*",)

    def test_single_string_normalizes_to_singleton_tuple(self, tmp_path: Path) -> None:
        """A bare string value (not a list) should normalise to a 1-tuple."""
        reg = self._registry_for(
            tmp_path,
            "    actions: []\n    skills_dirs: \"tools\"\n    allowed_mcp_tools: []\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.skills_dirs == ("tools",)

    def test_absent_fields_normalize_to_empty(self, tmp_path: Path) -> None:
        """Roles without skills_dirs / allowed_mcp_tools keys must default to ()."""
        content = "roles:\n  minimal:\n    actions: [\"send\"]\n"
        p = _write_yaml(tmp_path, content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("minimal")
        assert role is not None
        assert role.skills_dirs == ()
        assert role.allowed_mcp_tools == ()

    def test_null_actions_normalizes_to_empty(self, tmp_path: Path) -> None:
        reg = self._registry_for(
            tmp_path,
            "    actions: null\n    skills_dirs: []\n    allowed_mcp_tools: []\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.actions == ()

    def test_multiple_dirs_preserved_in_order(self, tmp_path: Path) -> None:
        reg = self._registry_for(
            tmp_path,
            "    actions: []\n    skills_dirs: [\"a\", \"b\", \"c\"]\n    allowed_mcp_tools: []\n",
        )
        role = reg.get_role("test")
        assert role is not None
        assert role.skills_dirs == ("a", "b", "c")


# ---------------------------------------------------------------------------
# RoleConfig.prompt_path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPromptPath:
    def test_default_prompt_path_is_none(self) -> None:
        """RoleConfig.prompt_path must default to None."""
        cfg = RoleConfig()
        assert cfg.prompt_path is None

    def test_prompt_path_loaded_when_set(self, tmp_path: Path) -> None:
        """prompt_path is loaded verbatim when present in YAML."""
        yaml_content = "roles:\n  admin:\n    actions: []\n    prompt_path: \"roles/admin.md\"\n"
        p = _write_yaml(tmp_path, yaml_content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("admin")
        assert role is not None
        assert role.prompt_path == "roles/admin.md"

    def test_prompt_path_is_none_when_null_in_yaml(self, tmp_path: Path) -> None:
        """prompt_path is None when YAML value is null."""
        yaml_content = "roles:\n  user:\n    actions: []\n    prompt_path: null\n"
        p = _write_yaml(tmp_path, yaml_content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("user")
        assert role is not None
        assert role.prompt_path is None

    def test_prompt_path_is_none_when_absent(self, tmp_path: Path) -> None:
        """prompt_path is None when the key is not present in YAML."""
        yaml_content = "roles:\n  user:\n    actions: [\"send\"]\n"
        p = _write_yaml(tmp_path, yaml_content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("user")
        assert role is not None
        assert role.prompt_path is None

    def test_prompt_path_rejected_when_absolute(self, tmp_path: Path) -> None:
        """prompt_path is rejected (set to None) when it is an absolute path."""
        yaml_content = "roles:\n  admin:\n    actions: []\n    prompt_path: \"/etc/passwd\"\n"
        p = _write_yaml(tmp_path, yaml_content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("admin")
        assert role is not None
        assert role.prompt_path is None

    def test_prompt_path_rejected_when_traversal(self, tmp_path: Path) -> None:
        """prompt_path is rejected (set to None) when it contains '..' traversal."""
        yaml_content = "roles:\n  admin:\n    actions: []\n    prompt_path: \"../secret.md\"\n"
        p = _write_yaml(tmp_path, yaml_content)
        registry = RoleRegistry(config_path=p)
        role = registry.get_role("admin")
        assert role is not None
        assert role.prompt_path is None


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReload:
    def test_reload_reflects_changes(self, tmp_path: Path) -> None:
        """After reload(), changes written to disk are picked up."""
        p = _write_yaml(tmp_path, _FULL_YAML)
        registry = RoleRegistry(config_path=p)
        assert registry.get_role("admin") is not None

        # Overwrite the file with no roles
        p.write_text("roles: {}\n", encoding="utf-8")
        registry.reload()
        assert registry.get_role("admin") is None
