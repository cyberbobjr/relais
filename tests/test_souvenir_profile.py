"""Unit tests — load_profiles() delegation contract.

Verifies that load_profiles() delegates file lookup to resolve_config_path()
rather than maintaining its own private cascade, ensuring the RELAIS_HOME
environment variable is respected.
"""

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# load_profiles() delegates to resolve_config_path — no private cascade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_profiles_delegates_to_resolve_config_path(tmp_path: "pytest.TempPathFactory") -> None:
    """load_profiles() must delegate file lookup to resolve_config_path().

    This verifies that common.profile_loader does NOT contain its own private
    _CASCADE_DIRS / _find_config_file() cascade that bypasses the RELAIS_HOME
    environment variable.  The contract is: when resolve_config_path raises
    FileNotFoundError, load_profiles() (without an explicit config_path) must
    propagate that same error — proving that it called resolve_config_path
    rather than its own lookup logic.
    """
    from common.profile_loader import load_profiles

    with patch(
        "common.profile_loader.resolve_config_path",
        side_effect=FileNotFoundError("mocked cascade — no profiles.yaml found"),
    ) as mock_resolve:
        with pytest.raises(FileNotFoundError, match="mocked cascade"):
            load_profiles()

    mock_resolve.assert_called_once_with("profiles.yaml")


