"""Unit tests for atelier.mcp_loader — written TDD (RED first).

All tests use @pytest.mark.unit and run without network or filesystem side effects
beyond pytest's tmp_path fixture.
"""

import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from atelier.mcp_loader import McpServerConfig, load_for_sdk, load_mcp_servers


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

FULL_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    mcp_servers:
      global:
        - name: filesystem
          enabled: true
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        - name: web-search
          enabled: false
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-brave-search"]
          env:
            BRAVE_API_KEY: "${BRAVE_API_KEY}"
      contextual:
        - name: code-tools
          enabled: true
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-github"]
          profiles: [coder, precise]
          env:
            GITHUB_TOKEN: "${GITHUB_TOKEN}"
    """
)

ONLY_GLOBAL_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    mcp_servers:
      global:
        - name: filesystem
          enabled: true
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
      contextual: []
    """
)

NO_ENV_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    mcp_servers:
      global:
        - name: no-env-server
          enabled: true
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-test"]
      contextual: []
    """
)

SSE_YAML = textwrap.dedent(
    """\
    mcp_servers:
      global:
        - name: calendar
          enabled: true
          type: sse
          url: "http://127.0.0.1:8100"
        - name: search
          enabled: true
          type: sse
          url: "http://127.0.0.1:8101"
          env:
            API_KEY: "${SEARCH_API_KEY}"
      contextual: []
    """
)


@pytest.fixture()
def full_yaml(tmp_path: Path) -> Path:
    """Write the full YAML fixture (global + contextual) to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(FULL_YAML)
    return p


@pytest.fixture()
def only_global_yaml(tmp_path: Path) -> Path:
    """Write a YAML fixture with only global servers to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(ONLY_GLOBAL_YAML)
    return p


@pytest.fixture()
def no_env_yaml(tmp_path: Path) -> Path:
    """Write a YAML fixture where a server has no 'env' key to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(NO_ENV_YAML)
    return p


@pytest.fixture()
def sse_yaml(tmp_path: Path) -> Path:
    """Write a YAML fixture with SSE servers to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the written YAML file.
    """
    p = tmp_path / "mcp_servers.yaml"
    p.write_text(SSE_YAML)
    return p


# ---------------------------------------------------------------------------
# 1. Returns empty list when no config file found
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_empty_list_if_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_mcp_servers() returns [] gracefully when no config file exists in the cascade.

    All cascade directories are monkeypatched to nonexistent paths to ensure
    no real filesystem is touched and no exception is raised.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.mcp_loader as _mod
    from unittest.mock import patch

    with patch.object(_mod, "resolve_config_path", side_effect=FileNotFoundError):
        result = load_mcp_servers()

    assert result == []


# ---------------------------------------------------------------------------
# 2. Global enabled servers are included
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_global_enabled_servers_included(full_yaml: Path) -> None:
    """load_mcp_servers() includes global servers that have enabled: true.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(config_path=full_yaml)

    names = [s.name for s in result]
    assert "filesystem" in names


# ---------------------------------------------------------------------------
# 3. Global disabled servers are excluded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_global_disabled_servers_excluded(full_yaml: Path) -> None:
    """load_mcp_servers() excludes global servers that have enabled: false.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(config_path=full_yaml)

    names = [s.name for s in result]
    assert "web-search" not in names


# ---------------------------------------------------------------------------
# 4. Contextual server included when profile matches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contextual_included_when_profile_matches(full_yaml: Path) -> None:
    """load_mcp_servers() includes a contextual server when profile_name is in its profiles list.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(profile_name="coder", config_path=full_yaml)

    names = [s.name for s in result]
    assert "code-tools" in names


# ---------------------------------------------------------------------------
# 5. Contextual server excluded when profile does not match
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contextual_excluded_when_profile_no_match(full_yaml: Path) -> None:
    """load_mcp_servers() excludes a contextual server when profile_name is not in its profiles list.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(profile_name="fast", config_path=full_yaml)

    names = [s.name for s in result]
    assert "code-tools" not in names


# ---------------------------------------------------------------------------
# 6. Contextual included when profile_name is None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contextual_included_when_profile_none(full_yaml: Path) -> None:
    """load_mcp_servers() includes all enabled contextual servers when profile_name is None.

    When no profile is specified (None), contextual servers are included regardless
    of their profiles restriction.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(profile_name=None, config_path=full_yaml)

    names = [s.name for s in result]
    assert "code-tools" in names


# ---------------------------------------------------------------------------
# 7. Env values are left as-is (not expanded)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_values_left_as_is(full_yaml: Path) -> None:
    """load_mcp_servers() preserves '${VAR}' placeholders verbatim — no os.environ expansion.

    Environment variable substitution is intentionally deferred to subprocess spawn
    time; the loader must NOT call os.path.expandvars or similar.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_mcp_servers(profile_name="coder", config_path=full_yaml)

    code_tools = next(s for s in result if s.name == "code-tools")
    assert code_tools.env["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"


# ---------------------------------------------------------------------------
# 8. McpServerConfig is frozen (immutable)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_server_config_is_frozen() -> None:
    """McpServerConfig raises FrozenInstanceError on any attribute mutation.

    Frozen dataclasses guarantee immutability in transit through the pipeline.
    """
    config = McpServerConfig(
        name="test",
        type="stdio",
        command="npx",
        args=["-y", "some-package"],
        env={},
    )

    with pytest.raises(FrozenInstanceError):
        config.name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 9. Empty env defaults to empty dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_env_defaults_to_empty_dict(no_env_yaml: Path) -> None:
    """load_mcp_servers() sets env to {} when a server entry has no 'env' key in YAML.

    Args:
        no_env_yaml: Fixture path to a YAML file where the server has no 'env' entry.
    """
    result = load_mcp_servers(config_path=no_env_yaml)

    assert len(result) == 1
    server = result[0]
    assert server.name == "no-env-server"
    assert server.env == {}


# ---------------------------------------------------------------------------
# 10. Config found via cascade (no explicit config_path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_found_via_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_mcp_servers() discovers the config through the cascade when config_path is None.

    The cascade is monkeypatched to a single tmp_path directory that contains a
    valid mcp_servers.yaml, confirming the cascade lookup path is exercised.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.mcp_loader as _mod
    from unittest.mock import patch

    config_file = tmp_path / "mcp_servers.yaml"
    config_file.write_text(ONLY_GLOBAL_YAML)

    with patch.object(_mod, "resolve_config_path", return_value=config_file):
        result = load_mcp_servers()

    assert any(s.name == "filesystem" for s in result)


# ---------------------------------------------------------------------------
# 11. Contextual disabled server excluded
# ---------------------------------------------------------------------------

CONTEXTUAL_DISABLED_YAML = textwrap.dedent(
    """\
    timeout: 10
    max_tools: 20
    mcp_servers:
      global: []
      contextual:
        - name: disabled-contextual
          enabled: false
          type: stdio
          command: npx
          args: ["-y", "@modelcontextprotocol/server-test"]
          profiles: [coder]
    """
)


@pytest.mark.unit
def test_contextual_disabled_server_excluded(tmp_path: Path) -> None:
    """load_mcp_servers() excludes contextual servers that have enabled: false.

    Verifies the enabled-gate inside the contextual loop is exercised.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    config_file = tmp_path / "mcp_servers.yaml"
    config_file.write_text(CONTEXTUAL_DISABLED_YAML)

    result = load_mcp_servers(profile_name="coder", config_path=config_file)

    assert result == []


# ---------------------------------------------------------------------------
# 12. load_for_sdk returns empty dict when no config file found
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_returns_empty_dict_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_for_sdk() returns {} when no mcp_servers.yaml exists in the cascade.

    Args:
        monkeypatch: Pytest fixture for safe attribute patching.
    """
    import atelier.mcp_loader as _mod
    from unittest.mock import patch

    with patch.object(_mod, "resolve_config_path", side_effect=FileNotFoundError):
        result = load_for_sdk()

    assert result == {}


# ---------------------------------------------------------------------------
# 13. load_for_sdk returns correct dict format for stdio servers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_returns_correct_format(full_yaml: Path) -> None:
    """load_for_sdk() returns a dict mapping server name to its config dict.

    For stdio servers the expected format is:
        {"server_name": {"type": "stdio", "command": "...", "args": [...]}}

    The 'env' key is present only when the server's env dict is non-empty.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_for_sdk(config_path=full_yaml)

    # 'filesystem' is global + enabled; 'web-search' is global + disabled
    assert "filesystem" in result
    assert "web-search" not in result

    fs = result["filesystem"]
    assert fs["type"] == "stdio"
    assert fs["command"] == "npx"
    assert fs["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    # filesystem has no env in the fixture
    assert "env" not in fs


# ---------------------------------------------------------------------------
# 14. load_for_sdk includes env key only when env is non-empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_env_included_when_non_empty(full_yaml: Path) -> None:
    """load_for_sdk() includes the 'env' key only for servers that have env vars.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_for_sdk(profile="coder", config_path=full_yaml)

    assert "code-tools" in result
    ct = result["code-tools"]
    assert "env" in ct
    assert ct["env"] == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}


# ---------------------------------------------------------------------------
# 15. load_for_sdk with profile filters contextual servers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_filters_by_profile(full_yaml: Path) -> None:
    """load_for_sdk() excludes contextual servers that don't match the given profile.

    Args:
        full_yaml: Fixture path to the temporary YAML file with global/contextual servers.
    """
    result = load_for_sdk(profile="fast", config_path=full_yaml)

    assert "code-tools" not in result
    assert "filesystem" in result


# ---------------------------------------------------------------------------
# 16. load_for_sdk with no env server omits env key
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_omits_env_key_when_empty(no_env_yaml: Path) -> None:
    """load_for_sdk() omits the 'env' key entirely when the server has no env vars.

    Args:
        no_env_yaml: Fixture path to a YAML file where the server has no 'env' entry.
    """
    result = load_for_sdk(config_path=no_env_yaml)

    assert "no-env-server" in result
    assert "env" not in result["no-env-server"]


# ---------------------------------------------------------------------------
# 17. SSE server config is loaded correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sse_server_config_loaded(sse_yaml: Path) -> None:
    """load_mcp_servers() populates type, url, and leaves command=None for SSE servers.

    Args:
        sse_yaml: Fixture path to a YAML file with SSE server definitions.
    """
    result = load_mcp_servers(config_path=sse_yaml)

    assert len(result) == 2
    cal = next(s for s in result if s.name == "calendar")
    assert cal.type == "sse"
    assert cal.url == "http://127.0.0.1:8100"
    assert cal.command is None
    assert cal.args == []


# ---------------------------------------------------------------------------
# 18. load_for_sdk returns correct format for SSE servers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_sse_server_format(sse_yaml: Path) -> None:
    """load_for_sdk() returns {type, url} for SSE servers and omits command/args.

    Args:
        sse_yaml: Fixture path to a YAML file with SSE server definitions.
    """
    result = load_for_sdk(config_path=sse_yaml)

    assert "calendar" in result
    cal = result["calendar"]
    assert cal["type"] == "sse"
    assert cal["url"] == "http://127.0.0.1:8100"
    assert "command" not in cal
    assert "args" not in cal
    assert "env" not in cal


# ---------------------------------------------------------------------------
# 19. load_for_sdk includes env for SSE servers when non-empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_for_sdk_sse_env_included_when_non_empty(sse_yaml: Path) -> None:
    """load_for_sdk() includes the 'env' key for SSE servers that have env vars.

    Args:
        sse_yaml: Fixture path to a YAML file with SSE server definitions.
    """
    result = load_for_sdk(config_path=sse_yaml)

    assert "search" in result
    search = result["search"]
    assert search["type"] == "sse"
    assert "env" in search
    assert search["env"] == {"API_KEY": "${SEARCH_API_KEY}"}
