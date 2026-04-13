"""Wrapper script for supervisord — checks prerequisites before launching baileys-api."""

import os
import shutil
import sys

from common.config_loader import get_relais_home


def main() -> None:
    """Check prerequisites and exec into bun to run baileys-api.

    Exits cleanly (code 0) if prerequisites are missing, so supervisord
    does not enter a crash loop.
    """
    vendor_dir = os.path.join(get_relais_home(), "vendor", "baileys-api")
    if not os.path.isdir(vendor_dir):
        print(
            f"baileys-api not installed at {vendor_dir}. "
            "Run: python -m channels.whatsapp install",
            file=sys.stderr,
        )
        sys.exit(0)

    bun = shutil.which("bun")
    if not bun:
        print(
            "bun not found in PATH. "
            "Install: curl -fsSL https://bun.sh/install | bash",
            file=sys.stderr,
        )
        sys.exit(0)

    os.chdir(vendor_dir)
    os.execvp(bun, [bun, "start"])


if __name__ == "__main__":
    main()
