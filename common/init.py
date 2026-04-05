import shutil
import os
from pathlib import Path
from .config_loader import get_relais_home

# Default template files shipped with the system installation
# Format: (destination_relative_path, source_relative_path_in_system)
DEFAULT_FILES = [
    ("config/config.yaml",                    "config/config.yaml.default"),
    ("config/atelier/profiles.yaml",          "config/atelier/profiles.yaml.default"),
    ("config/portail.yaml",                   "config/portail.yaml.default"),
    ("config/sentinelle.yaml",                "config/sentinelle.yaml.default"),
    ("config/atelier/mcp_servers.yaml",       "config/atelier/mcp_servers.yaml.default"),
    ("config/atelier.yaml",                   "config/atelier.yaml.default"),
    ("config/atelier/subagents/relais-config/subagent.yaml", "config/atelier/subagents/relais-config/subagent.yaml.default"),
    ("config/HEARTBEAT.md",                   "config/HEARTBEAT.md.default"),
    # Soul personality (Layer 1) — under prompts/ so soul_assembler can find it
    ("prompts/soul/SOUL.md",                         "prompts/soul/SOUL.md.default"),
    # Channel formatting overlays (Layer 4) — named {channel}_default.md
    ("prompts/channels/whatsapp_default.md",  "prompts/channels/whatsapp_default.md"),
    ("prompts/channels/telegram_default.md",  "prompts/channels/telegram_default.md"),
    # Reply-policy overlays (Layer 5)
    ("prompts/policies/in_meeting.md",   "prompts/policies/in_meeting.md"),
    ("prompts/policies/vacation.md",     "prompts/policies/vacation.md"),
    ("prompts/policies/out_of_hours.md", "prompts/policies/out_of_hours.md"),
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
        "config",
        "config/atelier",
        "config/atelier/subagents",
        "config/atelier/subagents/relais-config",
        "prompts/soul/variants",
        "prompts/channels",
        "prompts/policies",
        "prompts/roles",
        "prompts/users",
        "skills",
        "media", "logs", "backup", "storage",
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
