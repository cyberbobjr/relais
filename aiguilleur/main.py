"""Aiguilleur brick — unified channel adapter supervisor.

Functional role
---------------
Entry point for all external channel integrations (Discord, Telegram, etc.).
Loads channel definitions from aiguilleur.yaml and spawns one adapter per
enabled channel.  Acts as the ingestion boundary: each adapter translates
external API events into Envelope messages and pushes them onto the Redis bus.

Technical overview
------------------
``AiguilleurManager`` reads aiguilleur.yaml and instantiates either:

* ``NativeAiguilleur`` — Python adapters (e.g. DiscordAiguilleur) run in a
  dedicated OS thread via ``asyncio.run``.
* ``ExternalAiguilleur`` — non-Python adapters launched as subprocesses via
  ``subprocess.Popen``.

Automatic restart uses exponential backoff: ``min(2 ** restart_count, 30)``
seconds, up to 5 restarts per channel before the adapter is marked as failed.

Each adapter stamps context keys under ``CTX_AIGUILLEUR`` on every outbound envelope:

* ``envelope.context[CTX_AIGUILLEUR]["channel_profile"]`` — from ``ChannelConfig.profile``
  (aiguilleur.yaml) → ``get_default_llm_profile()`` (config.yaml:
  llm.default_profile) → ``"default"`` (resolved by Portail).
* ``envelope.context[CTX_AIGUILLEUR]["channel_prompt_path"]`` — from
  ``ChannelConfig.prompt_path`` (aiguilleur.yaml).  ``None`` when the channel
  has no ``prompt_path`` configured; in that case no channel formatting
  overlay is loaded by Atelier.
* ``envelope.context[CTX_AIGUILLEUR]["streaming"]`` — ``bool`` from
  ``ChannelConfig.streaming`` (aiguilleur.yaml); read by Atelier to decide
  whether to stream tokens to ``relais:messages:streaming:{channel}:{corr_id}``
  or publish a single outgoing envelope after the full reply is assembled.

Redis channels
--------------
Produced (by each adapter):
  - relais:messages:incoming:{channel}  — one stream per enabled channel

Consumed (by the corresponding relay adapter process):
  - relais:messages:outgoing:{channel}  — outbound replies routed back to the
    external API by the same adapter

Processing flow
---------------
  (1) Load aiguilleur.yaml; skip disabled entries.
  (2) For each enabled channel: instantiate NativeAiguilleur or
      ExternalAiguilleur depending on the adapter type.
  (3) Start all adapters (threads / subprocesses) concurrently.
  (4) Start background config-watcher daemon thread (watchfiles) that
      monitors aiguilleur.yaml for filesystem changes and calls
      ``_reload_channel_profiles()`` on every change.
  (5) Monitor adapter health; restart with exponential backoff on crash.
  (6) On SIGTERM / SIGINT: signal the config-watcher thread to stop,
      then stop all adapters and await clean exit.

Hot-reload — soft vs hard fields
---------------------------------
aiguilleur.yaml changes are classified into two categories:

* **Soft fields** (``profile``, ``prompt_path``, ``streaming``): applied
  live without restarting the adapter.  ``profile`` is updated through
  the ``ProfileRef`` object embedded in ``ChannelConfig`` so that all
  concurrent reader threads see the new value atomically.  Adapters read
  ``adapter.config`` on every inbound message so ``prompt_path`` and
  ``streaming`` are also effective immediately.

* **Hard fields** (``type``, ``class_path``, ``enabled``, ``command``):
  changing these emits a WARNING and requires a full process restart to
  take effect.  Adding or removing channels also requires a restart.
"""

import logging
from pathlib import Path

from common.brick_base import configure_logging_once

configure_logging_once()

from aiguilleur.core.manager import AiguilleurManager


def main() -> None:
    manager = AiguilleurManager()
    manager.run()


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    main()
