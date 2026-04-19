"""TDD tests for commandant/bundle_commands.py — Phase 4 of RELAIS bundle system.

Tests for all /bundle subcommands: install, uninstall, list, and error paths.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from common.bundle_errors import BundleValidationError, BundleNotFoundError, BundleInstallError
from common.bundles import BundleManifest
from common.envelope import Envelope
from common.envelope_actions import ACTION_MESSAGE_COMMAND
from common.streams import stream_outgoing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_envelope(content: str, channel: str = "discord") -> Envelope:
    """Create a test Envelope with the given content."""
    return Envelope(
        content=content,
        sender_id="discord:999",
        channel=channel,
        session_id="sess_test",
        correlation_id="corr_test",
        action=ACTION_MESSAGE_COMMAND,
    )


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Async Redis mock that records xadd calls."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1234-0")
    return redis


@pytest.fixture
def fake_manifest() -> BundleManifest:
    """A valid BundleManifest for use in tests."""
    return BundleManifest(
        name="my-bundle",
        description="A test bundle",
        version="1.0.0",
        author="Tester",
        tools=[],
    )


# ---------------------------------------------------------------------------
# Helper — extract reply content from xadd call
# ---------------------------------------------------------------------------


def _get_reply_content(mock_redis: AsyncMock) -> str:
    """Extract the content field from the first xadd payload."""
    assert mock_redis.xadd.called, "xadd was never called"
    call_args = mock_redis.xadd.call_args
    payload_json = call_args[0][1]["payload"]  # positional: (stream, fields)
    data = json.loads(payload_json)
    return data["content"]


def _get_reply_stream(mock_redis: AsyncMock) -> str:
    """Extract the stream name from the first xadd call."""
    call_args = mock_redis.xadd.call_args
    return call_args[0][0]


# ---------------------------------------------------------------------------
# test_bundle_install_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_install_success(mock_redis, fake_manifest):
    """/bundle install /path/to/file.zip → success reply published to outgoing stream."""
    envelope = _make_envelope("/bundle install /tmp/test.zip")

    with (
        patch("commandant.bundle_commands.install_bundle", return_value=fake_manifest) as mock_install,
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    mock_install.assert_called_once_with(Path("/tmp/test.zip"), Path("/fake/bundles"))
    reply = _get_reply_content(mock_redis)
    assert "my-bundle" in reply
    assert _get_reply_stream(mock_redis) == stream_outgoing("discord")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_install_missing_args(mock_redis):
    """/bundle install (no path) → usage help reply."""
    envelope = _make_envelope("/bundle install")

    with (
        patch("commandant.bundle_commands.install_bundle") as mock_install,
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    mock_install.assert_not_called()
    reply = _get_reply_content(mock_redis)
    assert "Usage" in reply or "usage" in reply


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_install_error(mock_redis):
    """/bundle install with BundleValidationError → clean error reply."""
    envelope = _make_envelope("/bundle install /tmp/bad.zip")

    with (
        patch("commandant.bundle_commands.install_bundle", side_effect=BundleValidationError("not a valid ZIP")),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "not a valid ZIP" in reply or "Error" in reply or "error" in reply


# ---------------------------------------------------------------------------
# test_bundle_uninstall_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_uninstall_success(mock_redis):
    """/bundle uninstall my-bundle → success reply published."""
    envelope = _make_envelope("/bundle uninstall my-bundle")

    with (
        patch("commandant.bundle_commands.uninstall_bundle") as mock_uninstall,
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    mock_uninstall.assert_called_once_with("my-bundle", Path("/fake/bundles"))
    reply = _get_reply_content(mock_redis)
    assert "my-bundle" in reply
    assert _get_reply_stream(mock_redis) == stream_outgoing("discord")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_uninstall_not_found(mock_redis):
    """/bundle uninstall missing-bundle → BundleNotFoundError → error reply."""
    envelope = _make_envelope("/bundle uninstall missing-bundle")

    with (
        patch("commandant.bundle_commands.uninstall_bundle", side_effect=BundleNotFoundError("bundle 'missing-bundle' is not installed")),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "missing-bundle" in reply or "not installed" in reply or "Error" in reply


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_uninstall_missing_args(mock_redis):
    """/bundle uninstall (no name) → usage help reply."""
    envelope = _make_envelope("/bundle uninstall")

    with (
        patch("commandant.bundle_commands.uninstall_bundle") as mock_uninstall,
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    mock_uninstall.assert_not_called()
    reply = _get_reply_content(mock_redis)
    assert "Usage" in reply or "usage" in reply


# ---------------------------------------------------------------------------
# test_bundle_list_empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_list_empty(mock_redis):
    """/bundle list with no installed bundles → 'No bundles installed' reply."""
    envelope = _make_envelope("/bundle list")

    with (
        patch("commandant.bundle_commands.list_bundles", return_value=[]),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "No bundles" in reply or "no bundles" in reply or "0" in reply


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_list_multiple(mock_redis):
    """/bundle list with 2 installed bundles → formatted reply listing both."""
    envelope = _make_envelope("/bundle list")

    manifests = [
        BundleManifest(name="alpha-bundle", description="Alpha bundle", version="1.0.0"),
        BundleManifest(name="beta-bundle", description="Beta bundle", version="2.1.0"),
    ]

    with (
        patch("commandant.bundle_commands.list_bundles", return_value=manifests),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "alpha-bundle" in reply
    assert "beta-bundle" in reply


# ---------------------------------------------------------------------------
# test_bundle_unknown_subcommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_unknown_subcommand(mock_redis):
    """/bundle foo → unknown subcommand → usage help reply."""
    envelope = _make_envelope("/bundle foo")

    from commandant.bundle_commands import handle_bundle
    await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "Usage" in reply or "usage" in reply or "install" in reply


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_no_subcommand(mock_redis):
    """/bundle (no subcommand) → usage help reply."""
    envelope = _make_envelope("/bundle")

    from commandant.bundle_commands import handle_bundle
    await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "Usage" in reply or "usage" in reply or "install" in reply


# ---------------------------------------------------------------------------
# test_bundle_registered_in_command_registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_registered_in_command_registry():
    """'bundle' must be present in KNOWN_COMMANDS after registration."""
    from commandant.commands import KNOWN_COMMANDS, COMMAND_REGISTRY
    assert "bundle" in KNOWN_COMMANDS
    assert "bundle" in COMMAND_REGISTRY


@pytest.mark.unit
def test_bundle_command_spec_is_valid():
    """The 'bundle' CommandSpec must have non-empty name and description."""
    from commandant.commands import COMMAND_REGISTRY
    spec = COMMAND_REGISTRY["bundle"]
    assert spec.name == "bundle"
    assert spec.description
    assert callable(spec.handler)


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_install_bundle_install_error(mock_redis):
    """/bundle install with BundleInstallError → clean error reply."""
    envelope = _make_envelope("/bundle install /tmp/ok.zip")

    with (
        patch("commandant.bundle_commands.install_bundle", side_effect=BundleInstallError("disk full")),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    reply = _get_reply_content(mock_redis)
    assert "disk full" in reply or "Error" in reply or "error" in reply


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_reply_goes_to_correct_channel(mock_redis, fake_manifest):
    """Reply must go to the channel in the envelope (not hardcoded)."""
    envelope = _make_envelope("/bundle list", channel="telegram")

    with (
        patch("commandant.bundle_commands.list_bundles", return_value=[fake_manifest]),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    assert _get_reply_stream(mock_redis) == stream_outgoing("telegram")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_bundle_install_action_is_message_outgoing(mock_redis, fake_manifest):
    """Reply envelope action must be ACTION_MESSAGE_OUTGOING."""
    from common.envelope_actions import ACTION_MESSAGE_OUTGOING
    envelope = _make_envelope("/bundle install /tmp/test.zip")

    with (
        patch("commandant.bundle_commands.install_bundle", return_value=fake_manifest),
        patch("commandant.bundle_commands.resolve_bundles_dir", return_value=Path("/fake/bundles")),
    ):
        from commandant.bundle_commands import handle_bundle
        await handle_bundle(envelope, mock_redis)

    payload_json = mock_redis.xadd.call_args[0][1]["payload"]
    data = json.loads(payload_json)
    assert data["action"] == ACTION_MESSAGE_OUTGOING
