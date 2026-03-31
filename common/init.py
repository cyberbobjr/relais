import shutil
import os
from pathlib import Path
from .config_loader import get_relais_home

# Default template files shipped with the system installation
# Format: (destination_relative_path, source_relative_path_in_system)
DEFAULT_FILES = [
    ("config/config.yaml",          "config/config.yaml.default"),
    ("config/profiles.yaml",        "config/profiles.yaml.default"),
    ("config/users.yaml",           "config/users.yaml.default"),
    ("config/reply_policy.yaml",    "config/reply_policy.yaml.default"),
    ("config/mcp_servers.yaml",     "config/mcp_servers.yaml.default"),
    ("config/HEARTBEAT.md",         "config/HEARTBEAT.md.default"),
    ("soul/SOUL.md",                "soul/SOUL.md.default"),
    ("soul/variants/SOUL_concise.md",       "soul/variants/SOUL_concise.md.default"),
    ("soul/variants/SOUL_professional.md",  "soul/variants/SOUL_professional.md.default"),
    # Prompt templates (no .default suffix — shipped as-is)
    ("prompts/whatsapp_default.md",  "prompts/whatsapp_default.md"),
    ("prompts/telegram_default.md",  "prompts/telegram_default.md"),
    ("prompts/out_of_hours.md",      "prompts/out_of_hours.md"),
    ("prompts/in_meeting.md",        "prompts/in_meeting.md"),
    ("prompts/vacation.md",          "prompts/vacation.md"),
]

def initialize_user_dir(system_install_path: Path | None = None):
    """Creates the RELAIS working directory structure on first run.

    Copies default templates from the system installation into the RELAIS home
    directory.  NEVER overwrites existing user files — safe to call on every
    startup.

    Args:
        system_install_path: Root of the system installation where template
            files live.  Defaults to the project root (parent of ``common/``).
    """
    if system_install_path is None:
        system_install_path = Path(__file__).parent.parent
    home = get_relais_home()

    # Create directory structure
    dirs = [
        "config", "soul/variants", "prompts",
        "skills/manual", "skills/auto",
        "media", "logs", "backup"
    ]
    for d in dirs:
        (home / d).mkdir(parents=True, exist_ok=True)

    # Copy defaults — only if file doesn't exist yet
    for dest_rel, src_rel in DEFAULT_FILES:
        dest = home / dest_rel
        src = system_install_path / src_rel
        if not dest.exists() and src.exists():
            shutil.copy(src, dest)

    # Create empty CLAUDE.md for skills registry if not present
    claude_md = home / "skills" / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# RELAIS Skills Registry\n\n"
            "## Skills actifs\n"
            "# Ajoutez vos skills ici — Le Forgeron met à jour automatiquement\n"
        )
