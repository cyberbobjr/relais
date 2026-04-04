"""Aiguilleur brick — unified channel adapter supervisor.

Functional role
---------------
Entry point for all external channel integrations (Discord, Telegram, etc.).
Loads channel definitions from channels.yaml and spawns one adapter per
enabled channel.  Acts as the ingestion boundary: each adapter translates
external API events into Envelope messages and pushes them onto the Redis bus.

Technical overview
------------------
``AiguilleurManager`` reads channels.yaml and instantiates either:

* ``NativeAiguilleur`` — Python adapters (e.g. DiscordAiguilleur) run in a
  dedicated OS thread via ``asyncio.run``.
* ``ExternalAiguilleur`` — non-Python adapters launched as subprocesses via
  ``subprocess.Popen``.

Automatic restart uses exponential backoff: ``min(2 ** restart_count, 30)``
seconds, up to 5 restarts per channel before the adapter is marked as failed.

Each adapter stamps two metadata keys on every outbound envelope:

* ``envelope.metadata["channel_profile"]`` — from ``ChannelConfig.profile``
  (channels.yaml) → ``get_default_llm_profile()`` (config.yaml:
  llm.default_profile) → ``"default"`` (resolved by Portail).
* ``envelope.metadata["channel_prompt_path"]`` — from
  ``ChannelConfig.prompt_path`` (channels.yaml).  ``None`` when the channel
  has no ``prompt_path`` configured; in that case no channel formatting
  overlay is loaded by Atelier.

Redis channels
--------------
Produced (by each adapter):
  - relais:messages:incoming:{channel}  — one stream per enabled channel

Consumed (by the corresponding relay adapter process):
  - relais:messages:outgoing:{channel}  — outbound replies routed back to the
    external API by the same adapter

Processing flow
---------------
  (1) Load channels.yaml; skip disabled entries.
  (2) For each enabled channel: instantiate NativeAiguilleur or
      ExternalAiguilleur depending on the adapter type.
  (3) Start all adapters (threads / subprocesses) concurrently.
  (4) Monitor adapter health; restart with exponential backoff on crash.
  (5) On SIGTERM / SIGINT: signal all adapters to stop, await clean exit.
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
