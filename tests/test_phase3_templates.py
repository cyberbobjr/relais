"""
Phase 3 — TDD tests for template files of the RELAIS system.

These tests verify:
- Existence on disk of all default files
- YAML validity of .yaml.default files
- Non-emptiness of Markdown files
- Behaviour of initialize_user_dir() — copy to a target directory
- Non-trivial content of SOUL.md.default (personality prompt)
"""
import shutil
from pathlib import Path

import pytest
import yaml

# Project root (two levels above tests/)
PROJECT_ROOT = Path(__file__).parent.parent

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _project_file(relative: str) -> Path:
    """Return the absolute path of a file within the project."""
    return PROJECT_ROOT / relative


# ──────────────────────────────────────────────────────────────────────────────
# 3.1  Default config/ files
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_YAML_DEFAULTS = [
    "config/config.yaml.default",
    "config/atelier/profiles.yaml.default",
    "config/portail.yaml.default",
    "config/sentinelle.yaml.default",
    "config/atelier/mcp_servers.yaml.default",
    "config/atelier.yaml.default",
]

CONFIG_MD_DEFAULTS = [
    "config/HEARTBEAT.md.default",
]


@pytest.mark.parametrize("rel_path", CONFIG_YAML_DEFAULTS)
def test_config_yaml_default_exists(rel_path: str) -> None:
    """Each default YAML file must exist on disk."""
    assert _project_file(rel_path).exists(), (
        f"Fichier manquant : {rel_path}"
    )


@pytest.mark.parametrize("rel_path", CONFIG_YAML_DEFAULTS)
def test_config_yaml_default_is_valid_yaml(rel_path: str) -> None:
    """Each default YAML file must be syntactically valid."""
    content = _project_file(rel_path).read_text(encoding="utf-8")
    # Must not raise an exception
    parsed = yaml.safe_load(content)
    assert parsed is not None, f"{rel_path} est vide ou nul"


@pytest.mark.parametrize("rel_path", CONFIG_MD_DEFAULTS)
def test_config_md_default_exists(rel_path: str) -> None:
    """Each default Markdown file must exist on disk."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", CONFIG_MD_DEFAULTS)
def test_config_md_default_is_non_empty(rel_path: str) -> None:
    """Each default Markdown file must contain non-empty content."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


# ──────────────────────────────────────────────────────────────────────────────
# 3.2  SOUL files in prompts/soul/
# ──────────────────────────────────────────────────────────────────────────────

SOUL_MD_DEFAULTS = [
    "prompts/soul/SOUL.md.default",
    "prompts/soul/variants/SOUL_concise.md.default",
    "prompts/soul/variants/SOUL_professional.md.default",
]


@pytest.mark.parametrize("rel_path", SOUL_MD_DEFAULTS)
def test_soul_md_default_exists(rel_path: str) -> None:
    """Each default SOUL file must exist on disk."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", SOUL_MD_DEFAULTS)
def test_soul_md_default_is_non_empty(rel_path: str) -> None:
    """Each default SOUL file must contain text."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


def test_soul_md_default_has_substantial_system_prompt() -> None:
    """SOUL.md.default must contain a non-trivial personality prompt (>100 chars)."""
    content = _project_file("prompts/soul/SOUL.md.default").read_text(encoding="utf-8").strip()
    assert len(content) > 100, (
        f"SOUL.md.default trop court ({len(content)} chars) — "
        "doit contenir un prompt de personnalité complet"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3.3  System prompts in prompts/
# ──────────────────────────────────────────────────────────────────────────────

PROMPT_FILES = [
    "prompts/channels/whatsapp_default.md",
    "prompts/channels/telegram_default.md",
    "prompts/policies/out_of_hours.md",
    "prompts/policies/in_meeting.md",
    "prompts/policies/vacation.md",
]


@pytest.mark.parametrize("rel_path", PROMPT_FILES)
def test_prompt_file_exists(rel_path: str) -> None:
    """Each prompt file must exist on disk."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", PROMPT_FILES)
def test_prompt_file_is_non_empty(rel_path: str) -> None:
    """Each prompt file must contain content."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


# ──────────────────────────────────────────────────────────────────────────────
# initialize_user_dir() — copy to tmp_path
# ──────────────────────────────────────────────────────────────────────────────

def test_initialize_user_dir_creates_directory_structure(tmp_path: Path) -> None:
    """initialize_user_dir() must create the standard directory structure."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        initialize_user_dir(system_install_path=PROJECT_ROOT)

        home = tmp_path / "relais_home"
        expected_dirs = [
            "config",
            "prompts/soul/variants", "prompts/channels", "prompts/policies",
            "prompts/roles", "prompts/users",
            "skills",
            "media", "logs", "backup",
        ]
        for d in expected_dirs:
            assert (home / d).is_dir(), f"Répertoire manquant : {d}"
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_copies_all_default_files(tmp_path: Path) -> None:
    """initialize_user_dir() must copy all files listed in DEFAULT_FILES."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir, DEFAULT_FILES
        initialize_user_dir(system_install_path=PROJECT_ROOT)

        home = tmp_path / "relais_home"
        for dest_rel, src_rel in DEFAULT_FILES:
            src = PROJECT_ROOT / src_rel
            if src.exists():
                dest = home / dest_rel
                assert dest.exists(), (
                    f"Fichier non copié : {dest_rel} (source : {src_rel})"
                )
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_does_not_overwrite_existing_files(tmp_path: Path) -> None:
    """initialize_user_dir() must NOT overwrite existing user files."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        home = tmp_path / "relais_home"
        (home / "config").mkdir(parents=True, exist_ok=True)

        # Pre-existing user file with custom content
        existing = home / "config" / "config.yaml"
        existing.write_text("# fichier utilisateur custom\nversion: custom\n")
        original_content = existing.read_text()

        initialize_user_dir(system_install_path=PROJECT_ROOT)

        # Content must not have changed
        assert existing.read_text() == original_content, (
            "initialize_user_dir() a écrasé un fichier utilisateur existant !"
        )
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_creates_skills_claude_md(tmp_path: Path) -> None:
    """initialize_user_dir() must create skills/CLAUDE.md with non-empty content."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        initialize_user_dir(system_install_path=PROJECT_ROOT)

        home = tmp_path / "relais_home"
        claude_md = home / "skills" / "CLAUDE.md"
        assert claude_md.exists(), "skills/CLAUDE.md non créé"
        assert len(claude_md.read_text().strip()) > 0, "skills/CLAUDE.md est vide"
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_idempotent(tmp_path: Path) -> None:
    """initialize_user_dir() may be called multiple times without error."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        # Double call — must not raise an exception
        initialize_user_dir(system_install_path=PROJECT_ROOT)
        initialize_user_dir(system_install_path=PROJECT_ROOT)
    finally:
        del os.environ["RELAIS_HOME"]


@pytest.mark.parametrize("rel_path", PROMPT_FILES)
def test_all_prompt_files_registered_in_default_files(rel_path: str) -> None:
    """Each file in PROMPT_FILES must be registered in DEFAULT_FILES.

    Regression: if a new prompt is added on disk but forgotten in
    DEFAULT_FILES, it will never be copied to ~/.relais/prompts/ on first
    launch.
    """
    from common.init import DEFAULT_FILES
    destinations = {dest for dest, _src in DEFAULT_FILES}
    assert rel_path in destinations, (
        f"{rel_path} existe sur disque mais n'est pas dans DEFAULT_FILES — "
        "il ne sera pas copié lors de initialize_user_dir()"
    )
