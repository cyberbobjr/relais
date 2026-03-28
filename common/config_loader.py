import os
from pathlib import Path
from dotenv import load_dotenv

# Search for .env from current directory upwards
load_dotenv()

def get_relais_home() -> Path:
    """
    Returns the RELAIS user directory.
    Override via RELAIS_HOME environment variable.
    """
    custom = os.environ.get("RELAIS_HOME")
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path.home() / ".relais").resolve()

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

def resolve_skills_dir() -> Path:
    """Skills directory is ALWAYS in user home."""
    path = get_relais_home() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path

def resolve_logs_dir() -> Path:
    """L'Archiviste always writes to user home logs."""
    path = get_relais_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path

def resolve_media_dir() -> Path:
    """Temporary media files — always in user home."""
    path = get_relais_home() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path

def resolve_storage_dir() -> Path:
    """Persistent storage (SQLite databases) — always in user home."""
    path = get_relais_home() / "storage"
    path.mkdir(parents=True, exist_ok=True)
    return path
