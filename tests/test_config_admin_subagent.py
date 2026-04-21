"""Unit tests for the relais-config subagent (YAML-based) and subagent registry.

Tests validate:
- AgentExecutor accepts and forwards subagents= and delegation_prompt=
- The relais-config YAML file exists and contains all required fields
- The system prompt contains all required config file paths, security rules, and skill management
- SubagentRegistry loads relais-config from YAML and filters by user_record
- _enrich_system_prompt appends delegation text when provided
- Atelier._handle_message uses the registry for subagent resolution
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from common.envelope import Envelope
from common.contexts import CTX_PORTAIL
from atelier.agent_executor import AgentResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Path to the shipped native YAML for relais-config (lives in the source tree, not copied to user dir)
_CONFIG_ADMIN_YAML = (
    Path(__file__).parent.parent / "atelier" / "subagents" / "relais-config" / "subagent.yaml"
)


def _load_config_admin_yaml() -> dict:
    """Load and return the relais-config YAML as a dict.

    Returns:
        Parsed YAML dict.
    """
    return yaml.safe_load(_CONFIG_ADMIN_YAML.read_text())


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
# config-admin YAML — schema validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_admin_yaml_file_exists() -> None:
    """The relais-config YAML default file must exist on disk."""
    assert _CONFIG_ADMIN_YAML.exists(), (
        f"Missing file: {_CONFIG_ADMIN_YAML}"
    )


@pytest.mark.unit
def test_config_admin_yaml_has_required_fields() -> None:
    """The YAML must contain name, description, and system_prompt."""
    data = _load_config_admin_yaml()
    assert data["name"] == "relais-config"
    assert "description" in data and data["description"]
    assert "system_prompt" in data and data["system_prompt"]


@pytest.mark.unit
def test_config_admin_yaml_name_field_is_config_admin() -> None:
    """The name field must equal 'relais-config' (matching the deployed filename stem)."""
    data = _load_config_admin_yaml()
    # When deployed, relais-config.yaml.default → relais-config.yaml, stem = relais-config
    assert data["name"] == "relais-config"


@pytest.mark.unit
def test_config_admin_yaml_has_delegation_snippet() -> None:
    """The YAML must include a delegation_snippet field."""
    data = _load_config_admin_yaml()
    assert "delegation_snippet" in data
    assert "relais-config" in data["delegation_snippet"]


# ---------------------------------------------------------------------------
# System prompt content — read from YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_admin_prompt_contains_all_config_paths() -> None:
    """System prompt must reference all config file paths."""
    data = _load_config_admin_yaml()
    prompt = data["system_prompt"]

    for path in ["portail.yaml", "sentinelle.yaml", "aiguilleur.yaml",
                  "profiles.yaml", "mcp_servers.yaml", "prompts/"]:
        assert path in prompt, f"Missing config path: {path}"


@pytest.mark.unit
def test_config_admin_prompt_contains_security_rules() -> None:
    """System prompt must include non-negotiable security constraints."""
    data = _load_config_admin_yaml()
    prompt = data["system_prompt"]

    assert "usr_system" in prompt
    assert "admin" in prompt


@pytest.mark.unit
def test_config_admin_prompt_contains_prompt_layers() -> None:
    """System prompt must document the 4 prompt overlay layers."""
    data = _load_config_admin_yaml()
    prompt = data["system_prompt"]

    for layer in ["soul/SOUL.md", "roles/", "users/", "channels/", "policies/"]:
        assert layer in prompt, f"Missing prompt layer: {layer}"


@pytest.mark.unit
def test_config_admin_prompt_contains_confirmation_protocol() -> None:
    """System prompt must enforce the read-diff-confirm-write protocol."""
    data = _load_config_admin_yaml()
    prompt_lower = data["system_prompt"].lower()

    assert "confirm" in prompt_lower
    assert "diff" in prompt_lower or "before" in prompt_lower


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_management() -> None:
    """System prompt must cover skill CRUD operations."""
    data = _load_config_admin_yaml()
    prompt_lower = data["system_prompt"].lower()

    for action in ["create", "modify", "delete"]:
        assert action in prompt_lower, f"Missing skill action: {action}"


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_md_format() -> None:
    """System prompt must document SKILL.md frontmatter fields."""
    data = _load_config_admin_yaml()
    prompt = data["system_prompt"]

    for field in ["name:", "description:", "allowed-tools:"]:
        assert field in prompt, f"Missing SKILL.md field: {field}"


@pytest.mark.unit
def test_config_admin_prompt_contains_skill_name_constraints() -> None:
    """System prompt must document skill naming rules."""
    data = _load_config_admin_yaml()
    prompt_lower = data["system_prompt"].lower()

    assert "lowercase" in prompt_lower or "a-z" in prompt_lower


@pytest.mark.unit
def test_config_admin_prompt_contains_skills_dir_and_registry() -> None:
    """System prompt must reference skills/ and CLAUDE.md registry."""
    data = _load_config_admin_yaml()
    prompt = data["system_prompt"]

    assert "skills/" in prompt
    assert "CLAUDE.md" in prompt


@pytest.mark.unit
def test_config_admin_prompt_contains_skills_dirs_reference() -> None:
    """System prompt must mention skills_dirs for role access control."""
    data = _load_config_admin_yaml()
    assert "skills_dirs" in data["system_prompt"]


# ---------------------------------------------------------------------------
# SubagentRegistry — YAML-based (loaded from tmp copy of shipped default)
# ---------------------------------------------------------------------------


def _make_registry_with_config_admin(tmp_path: Path) -> "SubagentRegistry":
    """Build a SubagentRegistry with relais-config loaded from a tmp dir.

    Copies the native subagent.yaml (from atelier/subagents/relais-config/) into a
    tmp user-tier directory so that SubagentRegistry.load() can find it via CONFIG_SEARCH_PATH=[tmp].

    Args:
        tmp_path: pytest tmp_path fixture value.

    Returns:
        A loaded SubagentRegistry containing relais-config.
    """
    import shutil
    from atelier.subagents import SubagentRegistry

    pack_dir = tmp_path / "config" / "atelier" / "subagents" / "relais-config"
    pack_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_CONFIG_ADMIN_YAML, pack_dir / "subagent.yaml")

    mock_tool_registry = MagicMock()
    mock_tool_registry.get = lambda name: None

    with (
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmp_path]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmp_path / "_nonexistent_native_"),
    ):
        return SubagentRegistry.load(mock_tool_registry)


@pytest.mark.unit
def test_registry_discovers_config_admin(tmp_path: Path) -> None:
    """SubagentRegistry.load() must find relais-config from the default YAML."""
    registry = _make_registry_with_config_admin(tmp_path)
    assert "relais-config" in registry.all_names


@pytest.mark.unit
def test_registry_specs_for_user_with_wildcard(tmp_path: Path) -> None:
    """User with allowed_subagents=["*"] gets all subagents."""
    registry = _make_registry_with_config_admin(tmp_path)

    ur = {"allowed_subagents": ["*"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) >= 1
    names = [s["name"] for s in specs]
    assert "relais-config" in names


@pytest.mark.unit
def test_registry_specs_for_user_with_explicit_name(tmp_path: Path) -> None:
    """User with allowed_subagents=["relais-config"] gets relais-config."""
    registry = _make_registry_with_config_admin(tmp_path)

    ur = {"allowed_subagents": ["relais-config"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) == 1
    assert specs[0]["name"] == "relais-config"


@pytest.mark.unit
def test_registry_specs_for_user_with_glob_pattern(tmp_path: Path) -> None:
    """User with allowed_subagents=["relais-*"] gets relais-config."""
    registry = _make_registry_with_config_admin(tmp_path)

    ur = {"allowed_subagents": ["relais-*"]}
    specs = registry.specs_for_user(ur)
    assert len(specs) == 1


@pytest.mark.unit
def test_registry_specs_for_user_with_empty_list(tmp_path: Path) -> None:
    """User with allowed_subagents=[] gets no subagents."""
    registry = _make_registry_with_config_admin(tmp_path)

    ur = {"allowed_subagents": []}
    specs = registry.specs_for_user(ur)
    assert specs == []


@pytest.mark.unit
def test_registry_specs_for_user_with_no_field(tmp_path: Path) -> None:
    """User record without allowed_subagents gets no subagents."""
    registry = _make_registry_with_config_admin(tmp_path)
    specs = registry.specs_for_user({})
    assert specs == []


@pytest.mark.unit
def test_registry_delegation_prompt_for_allowed_user(tmp_path: Path) -> None:
    """Delegation prompt for user with ["*"] contains relais-config snippet."""
    registry = _make_registry_with_config_admin(tmp_path)

    ur = {"allowed_subagents": ["*"]}
    prompt = registry.delegation_prompt_for_user(ur)
    assert "relais-config" in prompt
    assert "task()" in prompt


@pytest.mark.unit
def test_registry_delegation_prompt_empty_for_denied_user(tmp_path: Path) -> None:
    """Delegation prompt for user with [] is empty string."""
    registry = _make_registry_with_config_admin(tmp_path)

    prompt = registry.delegation_prompt_for_user({"allowed_subagents": []})
    assert prompt == ""


@pytest.mark.unit
def test_registry_parse_patterns_boundary() -> None:
    """_parse_subagent_patterns handles non-list inputs safely."""
    from atelier.subagents import _parse_subagent_patterns

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
        context={CTX_PORTAIL: {
            "user_record": ur,
            "llm_profile": "default",
            "user_id": "usr_test",
        }},
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

    The subagent registry is seeded with config-admin by pointing
    CONFIG_SEARCH_PATH at a temporary directory that contains a copy of
    the shipped default YAML.

    Returns:
        An Atelier instance safe for unit testing, with config-admin loaded.
    """
    import shutil
    import tempfile
    from atelier.main import Atelier

    # Prepare a tmp dir with relais-config pack so the registry picks it up
    tmpdir = Path(tempfile.mkdtemp())
    pack_dir = tmpdir / "config" / "atelier" / "subagents" / "relais-config"
    pack_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_CONFIG_ADMIN_YAML, pack_dir / "subagent.yaml")

    profile_mock = MagicMock()
    profile_mock.model = "test:model"
    profile_mock.max_turns = 10

    mock_saver = MagicMock()
    mock_saver_cls = MagicMock()
    mock_saver_cls.from_conn_string.return_value = mock_saver

    patchers = [
        patch("atelier.main.load_profiles", return_value={"default": profile_mock}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.resolve_profile", return_value=profile_mock),
        patch("atelier.main.AsyncSqliteSaver", new=mock_saver_cls),
        patch("atelier.subagents.CONFIG_SEARCH_PATH", [tmpdir]),
        patch("atelier.subagents.NATIVE_SUBAGENTS_PATH", tmpdir / "_nonexistent_native_"),
    ]
    for p in patchers:
        p.start()

    try:
        atelier = Atelier()
    except Exception:
        for p in patchers:
            try:
                p.stop()
            except RuntimeError:
                pass
        raise

    for p in patchers:
        try:
            p.stop()
        except RuntimeError:
            pass

    return atelier


async def _run_handle_message(allowed_subagents: list[str] | None) -> dict:
    """Run Atelier._handle_envelope and return AgentExecutor kwargs.

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
            return_value=AgentResult(reply_text="ok", messages_raw=[], tool_call_count=0, tool_error_count=0, subagent_traces=())
        )
        MockExecutor.return_value = mock_instance

        await atelier._handle_envelope(envelope, redis_conn)

    return MockExecutor.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("allowed,expected_count,has_delegation", [
    (["*"], 1, True),
    (["relais-config"], 1, True),
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
        assert subagents[0]["name"] == "relais-config"
    assert bool(delegation) == has_delegation
