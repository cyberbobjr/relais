import os
from pathlib import Path
from dotenv import load_dotenv

# Search for .env from current directory upwards
load_dotenv()

def get_relais_home() -> Path:
    """Returns the RELAIS working directory.

    Defaults to ``<project_root>/.relais`` where project root is the parent of
    the ``common/`` package.  Override via the ``RELAIS_HOME`` environment
    variable for system-wide installations or containerised deployments.

    Returns:
        Absolute, resolved path to the RELAIS home directory.
    """
    custom = os.environ.get("RELAIS_HOME")
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path(__file__).parent.parent / ".relais").resolve()

# Search path — user config always takes priority
CONFIG_SEARCH_PATH = [
    get_relais_home(),          # 1. ~/.relais/      (user — highest priority)
    Path("/opt/relais"),        # 2. /opt/relais/    (system installation)
    Path("./"),                 # 3. ./              (current dir — dev mode)
]

def resolve_config_path(filename: str) -> Path:
    """
    Resolves a config file using cascade priority.
    User config in ~/.relais/ always overrides system config.
    """
    # Try with 'config/' prefix if not present
    if not filename.startswith("config/"):
        filenames = [f"config/{filename}", filename]
    else:
        filenames = [filename]

    for fname in filenames:
        for base in CONFIG_SEARCH_PATH:
            candidate = base / fname
            if candidate.exists():
                return candidate

    raise FileNotFoundError(
        f"Config file '{filename}' not found.\n"
        f"Searched: {[str(p / filename) for p in CONFIG_SEARCH_PATH]}"
    )

def resolve_prompts_dir() -> Path:
    """Prompt templates directory.

    Searches the config cascade so users can override prompts in
    ``~/.relais/prompts/``.  Falls back to ``./prompts`` in dev mode.
    The directory is NOT auto-created here — it is initialised by
    ``initialize_user_dir`` on first run.
    """
    for base in CONFIG_SEARCH_PATH:
        candidate = base / "prompts"
        if candidate.is_dir():
            return candidate
    # No existing directory found — return the user-home path so callers
    # get a stable (even if empty) path rather than raising.
    return get_relais_home() / "prompts"


def resolve_skills_dir() -> Path:
    """Skills directory is ALWAYS in user home.

    The directory is NOT auto-created here — it is initialised by
    ``initialize_user_dir`` on first run.
    """
    return get_relais_home() / "skills"

def resolve_logs_dir() -> Path:
    """L'Archiviste always writes to user home logs.

    The directory is NOT auto-created here — it is initialised by
    ``initialize_user_dir`` on first run.
    """
    return get_relais_home() / "logs"

def resolve_media_dir() -> Path:
    """Temporary media files — always in user home.

    The directory is NOT auto-created here — it is initialised by
    ``initialize_user_dir`` on first run.
    """
    return get_relais_home() / "media"

def resolve_storage_dir() -> Path:
    """Persistent storage (SQLite databases) — always in user home.

    The directory is NOT auto-created here — it is initialised by
    ``initialize_user_dir`` on first run.
    """
    return get_relais_home() / "storage"
