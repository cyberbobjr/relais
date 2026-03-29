"""Tests for unknown_user_policy and profile-based guardrails in La Sentinelle.

Covers:
  - Part 1: ACLManager.check_unknown_user() with deny / guest / pending policies
  - Part 2: ProfileGuardrails.check() applying no_code_exec and no_external_links
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from atelier.profile_loader import ProfileConfig, ResilienceConfig


# ---------------------------------------------------------------------------
# Fixtures — in-memory YAML files so tests never hit the filesystem
# ---------------------------------------------------------------------------


def _make_users_yaml(extra_users: list[dict] | None = None) -> str:
    """Build a minimal users.yaml YAML string.

    Args:
        extra_users: Additional user dicts appended to the users list.

    Returns:
        YAML text ready to be written to a tmp file.
    """
    users: list[dict[str, Any]] = [
        {
            "id": "discord:111",
            "display_name": "Alice",
            "role": "user",
            "channels": ["discord"],
            "blocked": False,
            "llm_profile": "default",
        },
    ]
    if extra_users:
        users.extend(extra_users)

    data: dict[str, Any] = {
        "users": users,
        "roles": {
            "user": {"actions": ["send"]},
            "admin": {"actions": ["send", "admin"]},
        },
    }
    return yaml.dump(data)


def _make_profile(
    guardrails: tuple[str, ...] = (),
) -> ProfileConfig:
    """Return a minimal ProfileConfig for guardrail tests.

    Args:
        guardrails: Tuple of guardrail rule names to apply.

    Returns:
        A frozen ProfileConfig instance.
    """
    return ProfileConfig(
        model="test-model",
        temperature=0.7,
        max_tokens=512,
        resilience=ResilienceConfig(retry_attempts=1, retry_delays=[1]),
        guardrails=guardrails,
    )


# ---------------------------------------------------------------------------
# Part 1 — unknown_user_policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnknownUserPolicyDeny:
    """T1: unknown user + deny policy → returns False, no side effects."""

    def test_deny_returns_false(self, tmp_path: Path) -> None:
        """ACLManager.check_unknown_user returns False for deny policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="deny",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is False

    def test_deny_does_not_mutate_known_users(self, tmp_path: Path) -> None:
        """Known user is unaffected by deny policy for unknowns.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="deny",
        )

        assert mgr.is_allowed("discord:111", "discord") is True


@pytest.mark.unit
class TestUnknownUserPolicyGuest:
    """T2: unknown user + guest policy → returns True with guest_profile attached."""

    def test_guest_returns_true(self, tmp_path: Path) -> None:
        """ACLManager.is_allowed returns True for unknown user under guest policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="guest",
            guest_profile="fast",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is True

    def test_guest_profile_stored_in_result(self, tmp_path: Path) -> None:
        """get_effective_profile returns guest_profile for unknown user.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="guest",
            guest_profile="fast",
        )

        profile = mgr.get_effective_profile("discord:999")
        assert profile == "fast"

    def test_guest_known_user_keeps_own_profile(self, tmp_path: Path) -> None:
        """Known user under guest policy keeps their llm_profile, not guest_profile.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="guest",
            guest_profile="fast",
        )

        profile = mgr.get_effective_profile("discord:111")
        assert profile == "default"


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnknownUserPolicyPending:
    """T3: unknown user + pending policy → False + publishes to admin stream."""

    async def test_pending_returns_false(self, tmp_path: Path) -> None:
        """ACLManager.is_allowed returns False for unknown user under pending policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        result = mgr.is_allowed("discord:999", "discord")
        assert result is False

    async def test_pending_publishes_to_admin_stream(self, tmp_path: Path) -> None:
        """notify_pending publishes to relais:admin:pending_users stream.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await mgr.notify_pending(redis_mock, "discord:999", "discord")

        redis_mock.xadd.assert_awaited_once()
        call_args = redis_mock.xadd.call_args
        stream_name = call_args[0][0]
        assert stream_name == "relais:admin:pending_users"

    async def test_pending_payload_contains_user_id(self, tmp_path: Path) -> None:
        """Payload published to pending_users includes the unknown user_id.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(
            config_path=users_file,
            unknown_user_policy="pending",
        )

        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")

        await mgr.notify_pending(redis_mock, "discord:999", "discord")

        payload_dict = redis_mock.xadd.call_args[0][1]
        assert "discord:999" in str(payload_dict)


@pytest.mark.unit
class TestUnknownUserPolicyInvalid:
    """Raise ValueError on unsupported policy string."""

    def test_invalid_policy_raises_value_error(self, tmp_path: Path) -> None:
        """ACLManager raises ValueError for unknown policy string.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        with pytest.raises(ValueError, match="unknown_user_policy"):
            ACLManager(
                config_path=users_file,
                unknown_user_policy="allow_all_and_profit",
            )


# ---------------------------------------------------------------------------
# Part 2 — profile-based guardrails
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestProfileGuardrailsNoCodeExec:
    """T4/T5: no_code_exec guardrail rejects shell patterns, allows clean messages."""

    async def test_shell_subshell_is_rejected(self) -> None:
        """no_code_exec blocks messages containing $(...) subshell syntax."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("please run $(rm -rf /tmp/foo)", "discord:111")

        assert result.allowed is False
        assert result.reason is not None
        assert "no_code_exec" in result.reason

    async def test_subprocess_import_is_rejected(self) -> None:
        """no_code_exec blocks messages referencing subprocess module usage."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("use subprocess.run(['ls'])", "discord:111")

        assert result.allowed is False

    async def test_os_system_is_rejected(self) -> None:
        """no_code_exec blocks messages with os.system call."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("import os; os.system('ls')", "discord:111")

        assert result.allowed is False

    async def test_backtick_exec_is_rejected(self) -> None:
        """no_code_exec blocks backtick command execution patterns."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("run `whoami`", "discord:111")

        assert result.allowed is False

    async def test_eval_is_rejected(self) -> None:
        """no_code_exec blocks eval( call patterns."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("eval('print(1)')", "discord:111")

        assert result.allowed is False

    async def test_exec_is_rejected(self) -> None:
        """no_code_exec blocks exec( call patterns."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("exec(compiled_code)", "discord:111")

        assert result.allowed is False

    async def test_clean_message_is_allowed(self) -> None:
        """T5: Clean message passes no_code_exec guardrail without rejection."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("What is the capital of France?", "discord:111")

        assert result.allowed is True
        assert result.reason is None


@pytest.mark.unit
@pytest.mark.asyncio
class TestProfileGuardrailsNoRules:
    """T6: profile with no guardrails allows any message including shell patterns."""

    async def test_shell_patterns_allowed_when_no_guardrails(self) -> None:
        """Profile with empty guardrails tuple permits shell-pattern message."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=())
        checker = ProfileGuardrails(profile)

        result = await checker.check("run $(rm -rf /tmp) please", "discord:111")

        assert result.allowed is True

    async def test_url_allowed_when_no_guardrails(self) -> None:
        """Profile with empty guardrails tuple permits URL in message."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=())
        checker = ProfileGuardrails(profile)

        result = await checker.check("visit https://example.com", "discord:111")

        assert result.allowed is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestProfileGuardrailsNoExternalLinks:
    """T7/T8: no_external_links guardrail rejects URLs, ignores them when absent."""

    async def test_http_url_rejected(self) -> None:
        """T7: no_external_links blocks http:// URLs."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_external_links",))
        checker = ProfileGuardrails(profile)

        result = await checker.check(
            "check out http://evil.example.com/payload", "discord:111"
        )

        assert result.allowed is False
        assert result.reason is not None
        assert "no_external_links" in result.reason

    async def test_https_url_rejected(self) -> None:
        """no_external_links blocks https:// URLs."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_external_links",))
        checker = ProfileGuardrails(profile)

        result = await checker.check(
            "see https://docs.example.com/api", "discord:111"
        )

        assert result.allowed is False

    async def test_no_external_links_absent_allows_url(self) -> None:
        """T8: Profile without no_external_links permits URL in message."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec",))
        checker = ProfileGuardrails(profile)

        result = await checker.check(
            "see https://docs.example.com/api", "discord:111"
        )

        assert result.allowed is True

    async def test_plain_text_without_url_passes(self) -> None:
        """Message without URL passes no_external_links guardrail."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_external_links",))
        checker = ProfileGuardrails(profile)

        result = await checker.check("tell me about the weather", "discord:111")

        assert result.allowed is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestProfileGuardrailsMultipleRules:
    """Combined guardrails: first failing rule short-circuits."""

    async def test_first_failing_rule_stops_evaluation(self) -> None:
        """When both rules present, first match returns immediately with reason."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec", "no_external_links"))
        checker = ProfileGuardrails(profile)

        result = await checker.check("run $(curl https://example.com)", "discord:111")

        assert result.allowed is False
        # Either rule may fire first; important is that it stops
        assert result.reason is not None

    async def test_all_rules_must_pass_for_allowed(self) -> None:
        """Message must satisfy every active rule for allowed=True."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_code_exec", "no_external_links"))
        checker = ProfileGuardrails(profile)

        result = await checker.check("what time is it?", "discord:111")

        assert result.allowed is True


@pytest.mark.unit
class TestGuardrailResultImmutability:
    """GuardrailResult from guardrails.py must be immutable (frozen dataclass or NamedTuple)."""

    def test_guardrail_result_is_immutable(self) -> None:
        """Setting a field on GuardrailResult after construction raises an error."""
        from sentinelle.guardrails import GuardrailResult

        gr = GuardrailResult(allowed=True, reason=None)

        with pytest.raises((AttributeError, TypeError)):
            gr.allowed = False  # type: ignore[misc]


@pytest.mark.unit
class TestProfileGuardrailsUnknownRule:
    """ProfileGuardrails raises ValueError for unrecognised rule names."""

    def test_unknown_rule_raises_value_error(self) -> None:
        """ProfileGuardrails.__init__ raises ValueError for unrecognised rule."""
        from sentinelle.guardrails import ProfileGuardrails

        profile = _make_profile(guardrails=("no_such_rule",))

        with pytest.raises(ValueError, match="no_such_rule"):
            ProfileGuardrails(profile)


@pytest.mark.unit
class TestACLManagerGetEffectiveProfileFallback:
    """get_effective_profile fallback paths for deny/pending policies."""

    def test_deny_policy_returns_default_for_unknown(self, tmp_path: Path) -> None:
        """get_effective_profile returns 'default' for unknown user under deny policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(config_path=users_file, unknown_user_policy="deny")

        assert mgr.get_effective_profile("discord:999") == "default"

    def test_pending_policy_returns_default_for_unknown(self, tmp_path: Path) -> None:
        """get_effective_profile returns 'default' for unknown user under pending policy.

        Args:
            tmp_path: pytest built-in temporary directory.
        """
        from sentinelle.acl import ACLManager

        users_file = tmp_path / "users.yaml"
        users_file.write_text(_make_users_yaml(), encoding="utf-8")

        mgr = ACLManager(config_path=users_file, unknown_user_policy="pending")

        assert mgr.get_effective_profile("discord:999") == "default"
