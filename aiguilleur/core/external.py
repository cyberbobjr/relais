"""ExternalAiguilleur — adapter wrapping an external subprocess.

Used for channel adapters implemented in languages other than Python
(Node.js, Java, etc.).  The manager starts the process with Popen and
monitors it via poll().
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from aiguilleur.channel_config import ChannelConfig
from aiguilleur.core.base import BaseAiguilleur

logger = logging.getLogger(__name__)


class ExternalAiguilleur(BaseAiguilleur):
    """Wraps an external process as an Aiguilleur adapter.

    The command and args come from the ChannelConfig.  The process
    inherits the current environment (including Redis credentials).

    Example channels.yaml entry::

        whatsapp:
          enabled: true
          type: external
          command: node
          args:
            - adapters/whatsapp/index.js
    """

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)
        self._process: subprocess.Popen[bytes] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the external process."""
        if not self.config.command:
            raise ValueError(
                f"External adapter '{self.config.name}' has no command configured."
            )

        cmd = [self.config.command] + list(self.config.args)
        logger.info("Starting external adapter %s: %s", self.config.name, " ".join(cmd))
        self._process = subprocess.Popen(cmd)  # noqa: S603

    def stop(self, timeout: float = 8.0) -> None:
        """Send SIGTERM to the process and wait for it to exit."""
        if not self._process:
            return
        logger.info("Stopping external adapter %s (timeout=%.1fs)", self.config.name, timeout)
        self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "External adapter %s did not exit in %.1fs — sending SIGKILL",
                self.config.name,
                timeout,
            )
            self._process.kill()
            self._process.wait()

    def is_alive(self) -> bool:
        """Return True while the process has not terminated."""
        if self._process is None:
            return False
        return self._process.poll() is None
