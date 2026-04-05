"""Tests for user_id field in UserRecord and envelope.metadata stamping.

TDD — RED phase tests written before implementation.

Covers:
- UserRecord has a ``user_id`` field
- to_dict() includes ``user_id``
- from_dict() round-trips ``user_id`` correctly
- from_dict() with missing ``user_id`` falls back to ``""``
- _enrich_envelope() stamps ``envelope.metadata["user_id"]`` from YAML key
- _enrich_envelope() stamps ``envelope.metadata["user_record"]["user_id"]``
- Guest policy stamps ``envelope.metadata["user_id"] == "guest"``
- Guest user_record dict contains ``user_id == "guest"``
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from common.envelope import Envelope
from common.contexts import CTX_PORTAIL
from common.user_record import UserRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PORTAIL_YAML = dedent("""\
    unknown_user_policy: guest
    guest_role: guest

    users:
      usr_admin:
        display_name: "Admin User"
        role: admin
        blocked: false
        identifiers:
          discord:
            dm: "admin001"
      usr_alice:
        display_name: "Alice"
        role: user
        blocked: false
        identifiers:
          discord:
            dm: "alice001"

    roles:
      admin:
        actions: ["*"]
        skills_dirs: ["*"]
        allowed_mcp_tools: ["*"]

        prompt_path: null
      user:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []

        prompt_path: null
      guest:
        actions: []
        skills_dirs: []
        allowed_mcp_tools: []

        prompt_path: null
""")


def _write_yaml(tmp_path: Path, content: str = _PORTAIL_YAML) -> Path:
    """Write portail.yaml to tmp_path and return the path.

    Args:
        tmp_path: pytest temporary directory.
        content: YAML content string.

    Returns:
        Path to the written file.
    """
    p = tmp_path / "portail.yaml"
    p.write_text(content)
    return p


def _make_envelope(
    sender_id: str = "discord:admin001",
    channel: str = "discord",
) -> Envelope:
    """Build a minimal Envelope for testing.

    Args:
        sender_id: Originating user identifier.
        channel: Originating channel.

    Returns:
        A valid Envelope instance.
    """
    return Envelope(
        content="hello",
        sender_id=sender_id,
        channel=channel,
        session_id="sess-test",
        correlation_id="corr-test",
    )


def _make_portail(config_path: Path):
    """Instantiate a Portail with a fixed config path.

    Args:
        config_path: Path to portail.yaml.

    Returns:
        A Portail instance (no Redis connection needed for unit tests).
    """
    from unittest.mock import MagicMock
    from portail.main import Portail
    from portail.user_registry import UserRegistry

    registry = UserRegistry(config_path=config_path)
    portail = Portail.__new__(Portail)
    portail._user_registry = registry
    portail._unknown_user_policy = registry.unknown_user_policy
    portail._guest_role = registry.guest_role
    return portail


# ---------------------------------------------------------------------------
# UserRecord unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_record_has_user_id_field() -> None:
    """UserRecord must have a ``user_id`` string field.

    This is the first field in the frozen dataclass.
    """
    record = UserRecord(
        user_id="usr_admin",
        display_name="Admin",
        role="admin",
        blocked=False,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        allowed_subagents=[],
        prompt_path=None,
    )

    assert record.user_id == "usr_admin"


@pytest.mark.unit
def test_user_record_to_dict_includes_user_id() -> None:
    """to_dict() must include the ``user_id`` key."""
    record = UserRecord(
        user_id="usr_alice",
        display_name="Alice",
        role="user",
        blocked=False,
        actions=[],
        skills_dirs=[],
        allowed_mcp_tools=[],
        allowed_subagents=[],
        prompt_path=None,
    )

    result = record.to_dict()

    assert "user_id" in result
    assert result["user_id"] == "usr_alice"


@pytest.mark.unit
def test_user_record_from_dict_round_trips_user_id() -> None:
    """from_dict() must correctly restore ``user_id``."""
    original = UserRecord(
        user_id="usr_admin",
        display_name="Admin",
        role="admin",
        blocked=False,
        actions=["*"],
        skills_dirs=["*"],
        allowed_mcp_tools=["*"],
        allowed_subagents=[],
        prompt_path=None,
    )

    restored = UserRecord.from_dict(original.to_dict())

    assert restored.user_id == "usr_admin"
    assert restored == original


@pytest.mark.unit
def test_user_record_from_dict_missing_user_id_falls_back_to_empty() -> None:
    """from_dict() with no ``user_id`` key must fall back to ``""`` (backward compat)."""
    data = {
        "display_name": "Legacy",
        "role": "user",
        "blocked": False,
        "actions": [],
        "skills_dirs": [],
        "allowed_mcp_tools": [],

        "prompt_path": None,
    }

    record = UserRecord.from_dict(data)

    assert record.user_id == ""


# ---------------------------------------------------------------------------
# Portail._enrich_envelope — user_id stamping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_envelope_stamps_user_id_from_yaml_key(tmp_path: Path) -> None:
    """_enrich_envelope must stamp envelope.metadata["user_id"] with the YAML key.

    The YAML key (e.g. ``usr_admin``) is the stable cross-channel user_id.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    portail = _make_portail(_write_yaml(tmp_path))
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context.get(CTX_PORTAIL, {}).get("user_id") == "usr_admin"


@pytest.mark.unit
def test_enrich_envelope_user_record_dict_contains_user_id(tmp_path: Path) -> None:
    """user_record dict in envelope.metadata must contain ``user_id``.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    portail = _make_portail(_write_yaml(tmp_path))
    envelope = _make_envelope(sender_id="discord:admin001", channel="discord")

    portail._enrich_envelope(envelope)

    user_record = envelope.context[CTX_PORTAIL]["user_record"]
    assert user_record["user_id"] == "usr_admin"


@pytest.mark.unit
def test_enrich_envelope_stamps_user_id_for_second_user(tmp_path: Path) -> None:
    """_enrich_envelope stamps the correct YAML key for a non-admin user.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    portail = _make_portail(_write_yaml(tmp_path))
    envelope = _make_envelope(sender_id="discord:alice001", channel="discord")

    portail._enrich_envelope(envelope)

    assert envelope.context.get(CTX_PORTAIL, {}).get("user_id") == "usr_alice"
    assert envelope.context[CTX_PORTAIL]["user_record"]["user_id"] == "usr_alice"


@pytest.mark.unit
def test_enrich_envelope_unknown_user_does_not_stamp_user_id(tmp_path: Path) -> None:
    """_enrich_envelope must not stamp user_id for unresolved senders.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    yaml_deny = _PORTAIL_YAML.replace("unknown_user_policy: guest", "unknown_user_policy: deny")
    portail = _make_portail(_write_yaml(tmp_path, yaml_deny))
    envelope = _make_envelope(sender_id="discord:nobody", channel="discord")

    portail._enrich_envelope(envelope)

    assert CTX_PORTAIL not in envelope.context


# ---------------------------------------------------------------------------
# Portail._apply_guest_stamps — guest user_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guest_stamps_user_id_is_guest(tmp_path: Path) -> None:
    """_apply_guest_stamps must stamp envelope.metadata["user_id"] == "guest".

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    portail = _make_portail(_write_yaml(tmp_path))
    envelope = _make_envelope(sender_id="discord:unknown999", channel="discord")

    portail._apply_guest_stamps(envelope)

    assert envelope.context.get(CTX_PORTAIL, {}).get("user_id") == "guest"


@pytest.mark.unit
def test_guest_stamps_user_record_contains_user_id_guest(tmp_path: Path) -> None:
    """Guest user_record dict must contain ``user_id == "guest"``.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    portail = _make_portail(_write_yaml(tmp_path))
    envelope = _make_envelope(sender_id="discord:unknown999", channel="discord")

    portail._apply_guest_stamps(envelope)

    user_record = envelope.context[CTX_PORTAIL]["user_record"]
    assert user_record.get("user_id") == "guest"


# ---------------------------------------------------------------------------
# UserRegistry — YAML key captured as user_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_resolve_user_has_user_id(tmp_path: Path) -> None:
    """resolve_user() must return a UserRecord with user_id set to the YAML key.

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.user_registry import UserRegistry

    registry = UserRegistry(config_path=_write_yaml(tmp_path))
    record = registry.resolve_user("discord:admin001", channel="discord")

    assert record is not None
    assert record.user_id == "usr_admin"


@pytest.mark.unit
def test_registry_build_guest_record_has_user_id_guest(tmp_path: Path) -> None:
    """build_guest_record() must return a UserRecord with user_id == "guest".

    Args:
        tmp_path: pytest built-in temporary directory.
    """
    from portail.user_registry import UserRegistry

    registry = UserRegistry(config_path=_write_yaml(tmp_path))
    record = registry.build_guest_record()

    assert record.user_id == "guest"
