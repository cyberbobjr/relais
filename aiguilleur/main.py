"""AIGUILLEUR entry point — unified channel adapter supervisor.

Loads channel configurations from channels.yaml and starts one adapter
per enabled channel.  Handles SIGTERM/SIGINT for graceful shutdown.
"""

import logging
import os
import sys
from pathlib import Path

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)

from aiguilleur.core.manager import AiguilleurManager


def main() -> None:
    manager = AiguilleurManager()
    manager.run()


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    main()
