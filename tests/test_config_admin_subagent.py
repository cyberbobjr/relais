"""Unit tests for the config-admin subagent and subagent registry.

Tests validate:
- AgentExecutor accepts and forwards subagents= and delegation_prompt=
- The config-admin module exposes the subagent protocol (SPEC_NAME, build_spec, delegation_snippet)
- The system prompt contains all required config file paths, security rules, and skill management
- SubagentRegistry discovers config-admin and filters by user_record
- _enrich_system_prompt appends delegation text when provided
- Atelier._handle_message uses the registry for subagent resolution
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.envelope import Envelope
from atelier.agent_executor import AgentResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(model: str = "anthropic:claude-haiku-4-5") -> MagicMock:
    """Return a mock ProfileConfig.

    Args:
        model: The model identifier string.

    Returns:
        MagicMock with model, base_url, and api_key_env attributes.
    """
    profile = MagicMock()
    profile.model = model
    profile.base_url = None
    profile.api_key_env = None
    return profile


# ---------------------------------------------------------------------------
# AgentExecutor — subagents= and delegation_prompt= forwarding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executor_accepts_subagents_parameter() -> None:
    """AgentExecutor must accept a subagents= list without raising."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent):
        executor = AgentExecutor(
            profile=_make_profile(),
            soul_prompt="You are helpful.",
            tools=[],
            subagents=[{"name": "test", "description": "test"}],
        )
    assert executor is not None


@pytest.mark.unit
def test_executor_passes_subagents_to_create_deep_agent() -> None:
    """AgentExecutor must forward the subagents list to create_deep_agent."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()
    subagents = [{"name": "test-sub", "description": "A test subagent"}]

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
            subagents=subagents,
        )

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("subagents") == subagents


@pytest.mark.unit
def test_executor_defaults_subagents_to_empty_list() -> None:
    """AgentExecutor without subagents= must pass subagents=[] to create_deep_agent."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="...",
            tools=[],
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("subagents", "NOT_SET") == []


@pytest.mark.unit
def test_executor_passes_delegation_prompt_to_system_prompt() -> None:
    """AgentExecutor must inject delegation_prompt into the system prompt."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="Base.",
            tools=[],
            delegation_prompt="Delegate to config-admin.",
        )

    call_kwargs = mock_create.call_args.kwargs
    assert "Delegate to config-admin." in call_kwargs["system_prompt"]


@pytest.mark.unit
def test_executor_no_delegation_prompt_by_default() -> None:
    """AgentExecutor without delegation_prompt= must not inject delegation text."""
    from atelier.agent_executor import AgentExecutor

    mock_agent = MagicMock()

    with patch("atelier.agent_executor.create_deep_agent", return_value=mock_agent) as mock_create:
        AgentExecutor(
            profile=_make_profile(),
            soul_prompt="Base.",
            tools=[],
        )

    call_kwargs = mock_create.call_args.kwargs
    assert "task()" not in call_kwargs["system_prompt"]


# ---------------------------------------------------------------------------
# _enrich_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_system_prompt_adds_delegation_when_provided() -> None:
    """When delegation_prompt is non-empty, it is appended."""
    from atelier.agent_executor import _enrich_system_prompt

    result = _enrich_system_prompt("Base.", delegation_prompt="Delegate here.")
    assert "Delegate here." in result


@pytest.mark.unit
def test_enrich_system_prompt_no_delegation_when_empty() -> None:
    """When delegation_prompt is empty, no delegation text is appended."""
    from atelier.agent_executor import _enrich_system_prompt

    result = _enrich_system_prompt("Base.", delegation_prompt="")
    assert "Delegate" not in result


@pytest.mark.unit
def test_enrich_system_prompt_always_adds_memory_prompt() -> None:
    """Long-term memory prompt is always appended regardless of delegation."""
    from atelier.agent_executor import _enrich_system_prompt, LONG_TERM_MEMORY_PROMPT

    result = _enrich_system_prompt("Base.", delegation_prompt="")
    assert LONG_TERM_MEMORY_PROMPT in result

    result_with = _enrich_system_prompt("Base.", delegation_prompt="Some delegation.")
    assert LONG_TERM_MEMORY_PROMPT in result_with


@pytest.mark.unit
def test_enrich_system_prompt_no_duplicate_memory_prompt() -> None:
    """If memory prompt is already in soul_prompt, it is not duplicated."""
    from atelier.agent_executor import _enrich_system_prompt, LONG_TERM_MEMORY_PROMPT

    soul_with_memory = f"Soul.\n\n{LONG_TERM_MEMORY_PROMPT}"
    result = _enrich_system_prompt(soul_with_memory, delegation_prompt="")
    assert result.count(LONG_TERM_MEMORY_PROMPT) == 1


# ---------------------------------------------------------------------------
# config-admin module — subagent protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_admin_has_spec_name() -> None:
    """The config-admin module must expose SPEC_NAME = 'config-admin'."""
    from atelier.agents.config_admin import SPEC_NAME

    assert SPEC_NAME == "config-admin"


@pytest.mark.unit
def test_config_admin_build_spec_returns_dict() -> None:
    """build_spec() must return a dict with name, description, system_prompt."""
    from atelier.agents.config_admin import build_spec

    spec = build_spec()
    assert isinstance(spec, dict)
    assert spec["name"] == "config-admin"
    assert "description" in spec
    assert "system_prompt" in spec


@pytest.mark.unit
def test_config_admin_build_spec_no_model_key() -> None:
    """build_spec() must NOT include a model key (inherits from parent)."""
    from atelier.agents.config_admin import build_spec

    spec = build_spec()
    assert "model" not in spec


@pytest.mark.unit
def test_config_admin_delegation_snippet() -> None:
    """delegation_snippet() must return a non-empty string mentioning config-admin."""
    from atelier.agents.config_admin import delegation_snippet

    snippet = delegation_snippet()
    assert isinstance(snippet, str)
    assert "config-admin" in snippet
    assert len(snippet) > 20


# ---------------------------------------------------------------------------
# System prompt content
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_admin_prompt_contains_all_config_paths() -> None:
    """System prompt must reference all config file paths."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    for path in ["portail.yaml", "sentinelle.yaml", "channels.yaml",
                  "profiles.yaml", "mcp_servers.yaml", "prompts/"]:
        assert path in CONFIG_ADMIN_SYSTEM_PROMPT, (
            f"Missing config path: {path}"
        )


@pytest.mark.unit
def test_config_admin_prompt_contains_security_rules() -> None:
    """System prompt must include non-negotiable security constraints."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    assert "usr_system" in CONFIG_ADMIN_SYSTEM_PROMPT
    assert "admin" in CONFIG_ADMIN_SYSTEM_PROMPT


@pytest.mark.unit
def test_config_admin_prompt_contains_prompt_layers() -> None:
    """System prompt must document the 4 prompt overlay layers."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    for layer in ["soul/SOUL.md", "roles/", "users/", "channels/", "policies/"]:
        assert layer in CONFIG_ADMIN_SYSTEM_PROMPT, (
            f"Missing prompt layer: {layer}"
        )


@pytest.mark.unit
def test_config_admin_prompt_contains_confirmation_protocol() -> None:
    """System prompt must enforce the read-diff-confirm-write protocol."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    prompt_lower = CONFIG_ADMIN_SYSTEM_PROMPT.lower()
    assert "confirm" in prompt_lower
    assert "diff" in prompt_lower or "before" in prompt_lower


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_management() -> None:
    """System prompt must cover skill CRUD operations."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    prompt_lower = CONFIG_ADMIN_SYSTEM_PROMPT.lower()
    for action in ["create", "modify", "delete"]:
        assert action in prompt_lower, f"Missing skill action: {action}"


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_md_format() -> None:
    """System prompt must document SKILL.md frontmatter fields."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    for field in ["name:", "description:", "allowed-tools:"]:
        assert field in CONFIG_ADMIN_SYSTEM_PROMPT, (
            f"Missing SKILL.md field: {field}"
        )


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_name_constraints() -> None:
    """System prompt must document skill naming rules."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    prompt_lower = CONFIG_ADMIN_SYSTEM_PROMPT.lower()
    assert "lowercase" in prompt_lower or "a-z" in prompt_lower


@pytest.mark.unit
def test_config_admin_prompt_contains_skills_dir_and_registry() -> None:
    """System prompt must reference skills/ and CLAUDE.md registry."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    assert "skills/" in CONFIG_ADMIN_SYSTEM_PROMPT
    assert "CLAUDE.md" in CONFIG_ADMIN_SYSTEM_PROMPT


@pytest.mark.unit
def test_config_admin_prompt_contains_skills_dirs_reference() -> None:
    """System prompt must mention skills_dirs for role access control."""
    from atelier.agents.config_admin import CONFIG_ADMIN_SYSTEM_PROMPT

    assert "skills_dirs" in CONFIG_ADMIN_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# SubagentRegistry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_discovers_config_admin() -> None:
    """SubagentRegistry.discover() must find the config-admin module."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    assert "config-admin" in registry.all_names


@pytest.mark.unit
def test_registry_specs_for_user_with_wildcard() -> None:
    """User with allowed_subagents=["*"] gets all subagents."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    ur = {"allowed_subagents": ["*"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) >= 1
    assert specs[0]["name"] == "config-admin"


@pytest.mark.unit
def test_registry_specs_for_user_with_explicit_name() -> None:
    """User with allowed_subagents=["config-admin"] gets config-admin."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    ur = {"allowed_subagents": ["config-admin"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) == 1
    assert specs[0]["name"] == "config-admin"


@pytest.mark.unit
def test_registry_specs_for_user_with_glob_pattern() -> None:
    """User with allowed_subagents=["config-*"] gets config-admin."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    ur = {"allowed_subagents": ["config-*"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) == 1


@pytest.mark.unit
def test_registry_specs_for_user_with_empty_list() -> None:
    """User with allowed_subagents=[] gets no subagents."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    ur = {"allowed_subagents": []}
    specs = registry.specs_for_user(ur)
    assert specs == []


@pytest.mark.unit
def test_registry_specs_for_user_with_no_field() -> None:
    """User record without allowed_subagents gets no subagents."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    specs = registry.specs_for_user({})
    assert specs == []


@pytest.mark.unit
def test_registry_delegation_prompt_for_allowed_user() -> None:
    """Delegation prompt for user with ["*"] contains config-admin snippet."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    ur = {"allowed_subagents": ["*"]}
    prompt = registry.delegation_prompt_for_user(ur)
    assert "config-admin" in prompt
    assert "task()" in prompt


@pytest.mark.unit
def test_registry_delegation_prompt_empty_for_denied_user() -> None:
    """Delegation prompt for user with [] is empty string."""
    from atelier.agents import SubagentRegistry

    registry = SubagentRegistry.discover()
    prompt = registry.delegation_prompt_for_user({"allowed_subagents": []})
    assert prompt == ""


@pytest.mark.unit
def test_registry_skips_invalid_module() -> None:
    """Registry skips modules missing required attributes."""
    from atelier.agents._registry import _is_valid_subagent_module

    incomplete = MagicMock(spec=[])  # no attributes
    assert not _is_valid_subagent_module(incomplete)


@pytest.mark.unit
def test_registry_parse_patterns_boundary() -> None:
    """_parse_subagent_patterns handles non-list inputs safely."""
    from atelier.agents._registry import _parse_subagent_patterns

    assert _parse_subagent_patterns(None) == ()
    assert _parse_subagent_patterns("*") == ()
    assert _parse_subagent_patterns(42) == ()
    assert _parse_subagent_patterns(["*"]) == ("*",)
    assert _parse_subagent_patterns(["a", "b"]) == ("a", "b")


# ---------------------------------------------------------------------------
# Atelier._handle_message integration — registry-based gating
# ---------------------------------------------------------------------------


def _make_test_envelope(allowed_subagents: list[str] | None = None) -> Envelope:
    """Create an Envelope with a user_record carrying allowed_subagents.

    Args:
        allowed_subagents: The allowed_subagents list, or None to omit.

    Returns:
        A test Envelope with metadata set.
    """
    ur: dict = {
        "skills_dirs": [],
        "allowed_mcp_tools": [],
    }
    if allowed_subagents is not None:
        ur["allowed_subagents"] = allowed_subagents
    return Envelope(
        content="Hello",
        sender_id="discord:123",
        channel="discord",
        session_id="sess-test",
        correlation_id="corr-test",
        metadata={
            "user_record": ur,
            "llm_profile": "default",
            "user_id": "usr_test",
        },
    )


def _make_redis_mock() -> AsyncMock:
    """Create a fully mocked Redis connection.

    Returns:
        AsyncMock configured as a Redis async client.
    """
    redis_conn = AsyncMock()
    redis_conn.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis_conn.xadd = AsyncMock(return_value="0-0")
    redis_conn.xack = AsyncMock()
    redis_conn.publish = AsyncMock()
    return redis_conn


def _make_atelier_for_gating():
    """Instantiate Atelier with all __init__-time I/O patched out.

    Patches are started then stopped immediately after construction so
    the Atelier instance survives with its attributes set but no
    lingering mocks interfere with subsequent patches in test bodies.

    Returns:
        An Atelier instance safe for unit testing.
    """
    from atelier.main import Atelier

    profile_mock = MagicMock()
    profile_mock.model = "test:model"
    profile_mock.max_turns = 10

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    patches = {
        "atelier.main.load_profiles": {"default": profile_mock},
        "atelier.main.load_for_sdk": {},
        "atelier.main.resolve_profile": profile_mock,
    }
    active = {}
    for target, retval in patches.items():
        p = patch(target, return_value=retval)
        active[target] = p.start()
    p = patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls)
    active["saver"] = p.start()

    try:
        atelier = Atelier()
    except Exception:
        for v in active.values():
            v.stop()
        raise

    for v in active.values():
        try:
            v.stop()
        except RuntimeError:
            pass

    return atelier


async def _run_handle_message(allowed_subagents: list[str] | None) -> dict:
    """Run Atelier._handle_message and return AgentExecutor kwargs.

    Args:
        allowed_subagents: The allowed_subagents list for the user_record.

    Returns:
        The keyword arguments dict passed to AgentExecutor(...).
    """
    atelier = _make_atelier_for_gating()
    envelope = _make_test_envelope(allowed_subagents=allowed_subagents)
    redis_conn = _make_redis_mock()

    with (
        patch("atelier.main.AgentExecutor") as MockExecutor,
        patch("atelier.main.McpSessionManager", return_value=AsyncMock()),
        patch("atelier.main.make_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("atelier.main.resolve_profile", return_value=MagicMock(model="test:m")),
        patch("atelier.main.assemble_system_prompt", return_value="soul"),
        patch("atelier.main.load_for_sdk", return_value={}),
    ):
        mock_instance = AsyncMock()
        mock_instance.execute = AsyncMock(
            return_value=AgentResult(reply_text="ok", messages_raw=[])
        )
        MockExecutor.return_value = mock_instance

        await atelier._handle_message(redis_conn, "msg-1", envelope.to_json())

    return MockExecutor.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("allowed,expected_count,has_delegation", [
    (["*"], 1, True),
    (["config-admin"], 1, True),
    ([], 0, False),
    (None, 0, False),
])
async def test_handle_message_subagent_gating(
    allowed: list[str] | None,
    expected_count: int,
    has_delegation: bool,
) -> None:
    """Subagents and delegation prompt are filtered by allowed_subagents.

    Args:
        allowed: The allowed_subagents value in user_record.
        expected_count: Expected number of subagents passed to AgentExecutor.
        has_delegation: Whether delegation_prompt should be non-empty.
    """
    kwargs = await _run_handle_message(allowed)
    subagents = kwargs.get("subagents", [])
    delegation = kwargs.get("delegation_prompt", "")
    assert len(subagents) == expected_count
    if expected_count > 0:
        assert subagents[0]["name"] == "config-admin"
    assert bool(delegation) == has_delegation
