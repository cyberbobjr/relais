"""Reply policy loader for Le Portail.

Determines whether an automatic reply should be sent for a given envelope,
based on rules declared in reply_policy.yaml.
"""

import logging
from pathlib import Path
from typing import Any

from common.config_loader import get_relais_home
from common.envelope import Envelope

logger = logging.getLogger("portail.reply_policy")

_USER_POLICY_PATH = get_relais_home() / "config" / "reply_policy.yaml"
_DEFAULT_POLICY_PATH = Path(__file__).parent.parent / "config" / "reply_policy.yaml.default"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict.

    Tries PyYAML first, falls back to a minimal JSON-compatible loader if
    PyYAML is not installed.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        Parsed dictionary, or empty dict on parse error.
    """
    try:
        import yaml  # type: ignore[import-untyped]
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        import json
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("Failed to parse policy file %s: %s", path, exc)
        return {}


class ReplyPolicy:
    """Loads and evaluates the auto-reply policy for Le Portail.

    Policy file resolution order:
      1. ~/.relais/config/reply_policy.yaml  (user overrides)
      2. config/reply_policy.yaml.default  (repo defaults)
      3. Built-in fallback: allow everything.

    The policy YAML structure is intentionally open-ended; only the keys
    consumed by ``should_reply`` are documented here.  Unknown keys are
    preserved and returned by ``get_policy`` for future use.

    Expected YAML shape (all keys optional)::

        enabled: true          # master switch; false → never reply
        channels:              # whitelist of channels; absent → all allowed
          - discord
          - telegram
        blocked_users:         # sender_id values that are never auto-replied
          - "123456789"
    """

    def __init__(self) -> None:
        """Initialize and load the policy from disk."""
        self._policy: dict[str, Any] = {}
        self._policy_path: Path | None = None
        self.reload()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_reply(self, envelope: Envelope) -> bool:
        """Decide whether an automatic reply should be sent.

        Rules evaluated in order:
          1. ``enabled`` key — if explicitly False, no reply.
          2. ``channels`` whitelist — envelope.channel must be listed.
          3. ``blocked_users`` — envelope.sender_id must not be listed.

        Args:
            envelope: The incoming message envelope to evaluate.

        Returns:
            True if an automatic reply is permitted, False otherwise.
        """
        if not self._policy:
            return True

        if not self._policy.get("enabled", True):
            return False

        allowed_channels: list[str] | None = self._policy.get("channels")
        if allowed_channels is not None and envelope.channel not in allowed_channels:
            logger.debug(
                "Reply blocked: channel %r not in whitelist", envelope.channel
            )
            return False

        blocked_users: list[str] = self._policy.get("blocked_users", [])
        if str(envelope.sender_id) in [str(u) for u in blocked_users]:
            logger.debug(
                "Reply blocked: sender %r is in blocked_users", envelope.sender_id
            )
            return False

        return True

    def get_policy(self) -> dict[str, Any]:
        """Return the raw policy dictionary.

        Returns:
            A shallow copy of the loaded policy dict.
        """
        return dict(self._policy)

    def reload(self) -> None:
        """Reload the policy from disk.

        Resolution order:
          1. ~/.relais/config/reply_policy.yaml
          2. config/reply_policy.yaml.default (repo default)
          3. Empty dict → allow everything (built-in fallback)
        """
        if _USER_POLICY_PATH.exists():
            self._policy_path = _USER_POLICY_PATH
        elif _DEFAULT_POLICY_PATH.exists():
            self._policy_path = _DEFAULT_POLICY_PATH
        else:
            logger.debug(
                "No reply_policy.yaml found; defaulting to allow-all."
            )
            self._policy = {}
            self._policy_path = None
            return

        self._policy = _load_yaml(self._policy_path)
        logger.info("Loaded reply policy from %s", self._policy_path)
