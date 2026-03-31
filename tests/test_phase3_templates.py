"""
Phase 3 — Tests TDD pour les fichiers templates du système RELAIS.

Ces tests vérifient :
- Existence sur disque de tous les fichiers default
- Validité YAML des fichiers .yaml.default
- Non-vacuité des fichiers Markdown
- Fonctionnement de initialize_user_dir() — copie vers un répertoire cible
- Contenu non-trivial de SOUL.md.default (prompt de personnalité)
"""
import shutil
from pathlib import Path

import pytest
import yaml

# Racine du projet (deux niveaux au-dessus de tests/)
PROJECT_ROOT = Path(__file__).parent.parent

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _project_file(relative: str) -> Path:
    """Retourne le chemin absolu d'un fichier dans le projet."""
    return PROJECT_ROOT / relative


# ──────────────────────────────────────────────────────────────────────────────
# 3.1  Fichiers default config/
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_YAML_DEFAULTS = [
    "config/config.yaml.default",
    "config/profiles.yaml.default",
    "config/users.yaml.default",
    "config/reply_policy.yaml.default",
    "config/mcp_servers.yaml.default",
]

CONFIG_MD_DEFAULTS = [
    "config/HEARTBEAT.md.default",
]


@pytest.mark.parametrize("rel_path", CONFIG_YAML_DEFAULTS)
def test_config_yaml_default_exists(rel_path: str) -> None:
    """Chaque fichier YAML default doit exister sur disque."""
    assert _project_file(rel_path).exists(), (
        f"Fichier manquant : {rel_path}"
    )


@pytest.mark.parametrize("rel_path", CONFIG_YAML_DEFAULTS)
def test_config_yaml_default_is_valid_yaml(rel_path: str) -> None:
    """Chaque fichier YAML default doit être syntaxiquement valide."""
    content = _project_file(rel_path).read_text(encoding="utf-8")
    # Ne doit pas lever d'exception
    parsed = yaml.safe_load(content)
    assert parsed is not None, f"{rel_path} est vide ou nul"


@pytest.mark.parametrize("rel_path", CONFIG_MD_DEFAULTS)
def test_config_md_default_exists(rel_path: str) -> None:
    """Chaque fichier Markdown default doit exister sur disque."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", CONFIG_MD_DEFAULTS)
def test_config_md_default_is_non_empty(rel_path: str) -> None:
    """Chaque fichier Markdown default doit contenir du contenu non-vide."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


# ──────────────────────────────────────────────────────────────────────────────
# 3.2  Fichiers SOUL dans prompts/soul/
# ──────────────────────────────────────────────────────────────────────────────

SOUL_MD_DEFAULTS = [
    "prompts/soul/SOUL.md.default",
    "prompts/soul/variants/SOUL_concise.md.default",
    "prompts/soul/variants/SOUL_professional.md.default",
]


@pytest.mark.parametrize("rel_path", SOUL_MD_DEFAULTS)
def test_soul_md_default_exists(rel_path: str) -> None:
    """Chaque fichier SOUL default doit exister sur disque."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", SOUL_MD_DEFAULTS)
def test_soul_md_default_is_non_empty(rel_path: str) -> None:
    """Chaque fichier SOUL default doit contenir du texte."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


def test_soul_md_default_has_substantial_system_prompt() -> None:
    """SOUL.md.default doit contenir un prompt de personnalité non-trivial (>100 chars)."""
    content = _project_file("prompts/soul/SOUL.md.default").read_text(encoding="utf-8").strip()
    assert len(content) > 100, (
        f"SOUL.md.default trop court ({len(content)} chars) — "
        "doit contenir un prompt de personnalité complet"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3.3  Prompts système dans prompts/
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
    """Chaque fichier prompt doit exister sur disque."""
    assert _project_file(rel_path).exists(), f"Fichier manquant : {rel_path}"


@pytest.mark.parametrize("rel_path", PROMPT_FILES)
def test_prompt_file_is_non_empty(rel_path: str) -> None:
    """Chaque fichier prompt doit contenir du contenu."""
    content = _project_file(rel_path).read_text(encoding="utf-8").strip()
    assert len(content) > 0, f"{rel_path} est vide"


# ──────────────────────────────────────────────────────────────────────────────
# initialize_user_dir() — copie vers tmp_path
# ──────────────────────────────────────────────────────────────────────────────

def test_initialize_user_dir_creates_directory_structure(tmp_path: Path) -> None:
    """initialize_user_dir() doit créer l'arborescence de répertoires standard."""
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
            "skills/manual", "skills/auto",
            "media", "logs", "backup",
        ]
        for d in expected_dirs:
            assert (home / d).is_dir(), f"Répertoire manquant : {d}"
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_copies_all_default_files(tmp_path: Path) -> None:
    """initialize_user_dir() doit copier tous les fichiers DEFAULT_FILES."""
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
    """initialize_user_dir() ne doit PAS écraser les fichiers utilisateur existants."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        home = tmp_path / "relais_home"
        (home / "config").mkdir(parents=True, exist_ok=True)

        # Fichier utilisateur pré-existant avec contenu custom
        existing = home / "config" / "config.yaml"
        existing.write_text("# fichier utilisateur custom\nversion: custom\n")
        original_content = existing.read_text()

        initialize_user_dir(system_install_path=PROJECT_ROOT)

        # Le contenu ne doit pas avoir changé
        assert existing.read_text() == original_content, (
            "initialize_user_dir() a écrasé un fichier utilisateur existant !"
        )
    finally:
        del os.environ["RELAIS_HOME"]


def test_initialize_user_dir_creates_skills_claude_md(tmp_path: Path) -> None:
    """initialize_user_dir() doit créer skills/CLAUDE.md avec un contenu non-vide."""
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
    """initialize_user_dir() peut être appelé plusieurs fois sans erreur."""
    import os
    os.environ["RELAIS_HOME"] = str(tmp_path / "relais_home")

    try:
        from common.init import initialize_user_dir
        # Double appel — ne doit pas lever d'exception
        initialize_user_dir(system_install_path=PROJECT_ROOT)
        initialize_user_dir(system_install_path=PROJECT_ROOT)
    finally:
        del os.environ["RELAIS_HOME"]


@pytest.mark.parametrize("rel_path", PROMPT_FILES)
def test_all_prompt_files_registered_in_default_files(rel_path: str) -> None:
    """Chaque fichier de PROMPT_FILES doit être enregistré dans DEFAULT_FILES.

    Régression : si un nouveau prompt est ajouté sur disque mais oublié dans
    DEFAULT_FILES, il ne sera jamais copié dans ~/.relais/prompts/ au premier
    lancement.
    """
    from common.init import DEFAULT_FILES
    destinations = {dest for dest, _src in DEFAULT_FILES}
    assert rel_path in destinations, (
        f"{rel_path} existe sur disque mais n'est pas dans DEFAULT_FILES — "
        "il ne sera pas copié lors de initialize_user_dir()"
    )
