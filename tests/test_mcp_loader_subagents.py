"""Unit tests for subagent loading in atelier.mcp_loader — written TDD (RED first).

Tests cover load_subagents() and load_subagents_for_sdk() functions that read
the subagents section from mcp_servers.yaml and convert them to AgentDefinition
instances for the claude-agent-sdk.

All tests use @pytest.mark.unit and operate on tmp_path fixtures only —
no real filesystem or network access.
"""

import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from atelier.mcp_loader import SubagentConfig, load_subagents, load_subagents_for_sdk


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

SUBAGENTS_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    global: []
    contextual: []
    subagents:
      - name: memory-retriever
        description: "Retrieve and store user memories, facts, and past interactions"
        enabled: true
      - name: web-searcher
        description: "Search the web for current information and documentation"
        enabled: false
      - name: code-explorer
        description: "Explore GitHub repositories, search code and issues"
        enabled: true
        tools: ["mcp__github__search_code", "mcp__github__search_repositories"]
    """
)

ALL_DISABLED_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    global: []
    contextual: []
    subagents:
      - name: memory-retriever
        description: "Retrieve and store user memories"
        enabled: false
      - name: web-searcher
        description: "Search the web"
        enabled: false
    """
)

NO_SUBAGENTS_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    global: []
    contextual: []
    """
)

MASTER_SWITCH_OFF_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    global: []
    contextual: []
    subagents:
      - name: memory-retriever
        description: "Retrieve and store user memories"
        enabled: true
    """
)

# Matching config.yaml with master switch disabled
CONFIG_SUBAGENTS_DISABLED = textwrap.dedent(
    """\
    redis:
      unix_socket: ~/.relais/redis.sock
      password: "${REDIS_PASSWORD}"
    subagents:
      enabled: false
    """
)

CONFIG_SUBAGENTS_ENABLED = textwrap.dedent(
    """\
    redis:
      unix_socket: ~/.relais/redis.sock
      password: "${REDIS_PASSWORD}"
    subagents:
      enabled: true
    """
)


@pytest.fixture()
def subagents_yaml(tmp_path: Path) -> Path:
    """Write the full subagents YAML fixture to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written mcp_servers YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(SUBAGENTS_YAML)
    return p


@pytest.fixture()
def all_disabled_yaml(tmp_path: Path) -> Path:
    """Write a YAML fixture with all subagents disabled to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written mcp_servers YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(ALL_DISABLED_YAML)
    return p


@pytest.fixture()
def no_subagents_yaml(tmp_path: Path) -> Path:
    """Write a YAML fixture with no subagents section to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written mcp_servers YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(NO_SUBAGENTS_YAML)
    return p


# ---------------------------------------------------------------------------
# T1: load_subagents returns only enabled subagents
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_returns_only_enabled(subagents_yaml: Path) -> None:
    """load_subagents() returns only subagents with enabled: true.

    The fixture has 3 subagents: memory-retriever (enabled), web-searcher (disabled),
    code-explorer (enabled). Only the two enabled ones should be returned.

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents(config_path=subagents_yaml)

    names = [s.name for s in result]
    assert "memory-retriever" in names
    assert "code-explorer" in names
    assert "web-searcher" not in names
    assert len(result) == 2


# ---------------------------------------------------------------------------
# T2: load_subagents with all disabled returns empty list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_all_disabled_returns_empty(all_disabled_yaml: Path) -> None:
    """load_subagents() returns [] when all subagents are disabled.

    Args:
        all_disabled_yaml: Fixture path to the YAML with all subagents disabled.
    """
    result = load_subagents(config_path=all_disabled_yaml)

    assert result == []


# ---------------------------------------------------------------------------
# T3: load_subagents with no subagents section returns empty list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_no_section_returns_empty(no_subagents_yaml: Path) -> None:
    """load_subagents() returns [] gracefully when the YAML has no subagents section.

    Args:
        no_subagents_yaml: Fixture path to the YAML without a subagents section.
    """
    result = load_subagents(config_path=no_subagents_yaml)

    assert result == []


# ---------------------------------------------------------------------------
# T4: load_subagents_for_sdk returns dict keyed by name
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_returns_dict_keyed_by_name(subagents_yaml: Path) -> None:
    """load_subagents_for_sdk() returns a dict with subagent names as keys.

    Only enabled subagents should appear as keys. The values should be
    AgentDefinition instances.

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents_for_sdk(config_path=subagents_yaml)

    assert isinstance(result, dict)
    assert "memory-retriever" in result
    assert "code-explorer" in result
    assert "web-searcher" not in result


# ---------------------------------------------------------------------------
# T5: load_subagents_for_sdk AgentDefinition has generated prompt from description
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_prompt_generated_from_description(subagents_yaml: Path) -> None:
    """load_subagents_for_sdk() generates the prompt from the description field.

    The prompt must be: f"You are a specialized subagent. Your role: {description}."

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents_for_sdk(config_path=subagents_yaml)

    memory_agent = result["memory-retriever"]
    expected_prompt = (
        "You are a specialized subagent. Your role: "
        "Retrieve and store user memories, facts, and past interactions."
    )
    assert memory_agent.prompt == expected_prompt


# ---------------------------------------------------------------------------
# T6: load_subagents_for_sdk AgentDefinition model is None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_model_is_none(subagents_yaml: Path) -> None:
    """load_subagents_for_sdk() creates AgentDefinitions with model=None.

    model=None causes the subagent to inherit the principal agent's model.

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents_for_sdk(config_path=subagents_yaml)

    for agent_def in result.values():
        assert agent_def.model is None


# ---------------------------------------------------------------------------
# T7: load_subagents_for_sdk AgentDefinition mcpServers is not set (or None)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_mcp_servers_is_none(subagents_yaml: Path) -> None:
    """load_subagents_for_sdk() creates AgentDefinitions with mcpServers=None.

    mcpServers=None causes the subagent to inherit all MCPs from the parent.

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents_for_sdk(config_path=subagents_yaml)

    for agent_def in result.values():
        assert agent_def.mcpServers is None


# ---------------------------------------------------------------------------
# T8: load_subagents_for_sdk respects tools field (None when not set in YAML)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_tools_respected(subagents_yaml: Path) -> None:
    """load_subagents_for_sdk() passes tools when set and None when absent.

    memory-retriever has no tools key → tools should be None.
    code-explorer has explicit tools → they should be passed through as a list.

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents_for_sdk(config_path=subagents_yaml)

    # memory-retriever has no tools in the fixture
    memory_agent = result["memory-retriever"]
    assert memory_agent.tools is None

    # code-explorer has explicit tools (stored as tuple internally, converted
    # to list when passed to AgentDefinition for SDK compatibility)
    code_agent = result["code-explorer"]
    assert code_agent.tools == [
        "mcp__github__search_code",
        "mcp__github__search_repositories",
    ]


# ---------------------------------------------------------------------------
# T9: load_subagents_for_sdk returns empty dict when master switch disabled
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_empty_when_master_switch_off(tmp_path: Path) -> None:
    """load_subagents_for_sdk() returns {} when subagents.enabled=false in config.yaml.

    Even when mcp_servers.yaml has enabled subagents, the master switch in
    config.yaml takes precedence and disables all subagents.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    mcp_path = tmp_path / "mcp_servers.yaml"
    mcp_path.write_text(MASTER_SWITCH_OFF_YAML)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_SUBAGENTS_DISABLED)

    result = load_subagents_for_sdk(
        config_path=mcp_path,
        config_yaml_path=config_path,
    )

    assert result == {}


# ---------------------------------------------------------------------------
# T10: load_subagents_for_sdk returns non-empty dict when master switch enabled
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_subagents_for_sdk_returns_agents_when_master_switch_on(
    tmp_path: Path,
) -> None:
    """load_subagents_for_sdk() returns a non-empty dict when subagents.enabled=true.

    When the master switch is explicitly enabled in config.yaml AND enabled
    subagents exist in mcp_servers.yaml, the function must return a non-empty
    dict of AgentDefinition instances.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    mcp_path = tmp_path / "mcp_servers.yaml"
    mcp_path.write_text(MASTER_SWITCH_OFF_YAML)  # has one enabled subagent

    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_SUBAGENTS_ENABLED)

    result = load_subagents_for_sdk(
        config_path=mcp_path,
        config_yaml_path=config_path,
    )

    assert len(result) > 0
    assert "memory-retriever" in result


# ---------------------------------------------------------------------------
# T11: SubagentConfig is frozen (immutable) and has no enabled field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_config_is_frozen_and_has_no_enabled_field() -> None:
    """SubagentConfig is a frozen dataclass without an enabled field.

    The enabled field was removed as it was always True for returned instances
    (filtering happens in load_subagents before construction).
    """
    from dataclasses import fields as dc_fields

    cfg = SubagentConfig(name="test-agent", description="A test agent")

    # Verify frozen — should raise on mutation attempt
    with pytest.raises(FrozenInstanceError):
        cfg.name = "mutated"  # type: ignore[misc]

    # Verify no 'enabled' field on the dataclass
    field_names = {f.name for f in dc_fields(cfg)}
    assert "enabled" not in field_names
    assert "name" in field_names
    assert "description" in field_names
    assert "tools" in field_names


# ---------------------------------------------------------------------------
# T12: SubagentConfig.tools stores as tuple when provided
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subagent_config_tools_stored_as_tuple(subagents_yaml: Path) -> None:
    """load_subagents() stores tools as a tuple for immutability.

    The YAML 'tools' list is converted to a tuple so that SubagentConfig
    remains fully immutable (lists are mutable and cannot be hashed).

    Args:
        subagents_yaml: Fixture path to the temporary mcp_servers YAML file.
    """
    result = load_subagents(config_path=subagents_yaml)

    code_explorer = next(s for s in result if s.name == "code-explorer")
    assert isinstance(code_explorer.tools, tuple)
    assert code_explorer.tools == (
        "mcp__github__search_code",
        "mcp__github__search_repositories",
    )
