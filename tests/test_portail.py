"""Unit tests for portail module: ReplyPolicy and prompt_loader."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from common.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(channel: str = "discord", sender_id: str = "user-1") -> Envelope:
    """Return a minimal Envelope for policy tests."""
    return Envelope(
        content="hello",
        sender_id=sender_id,
        channel=channel,
        session_id="sess-test",
    )


# ---------------------------------------------------------------------------
# ReplyPolicy tests
# ---------------------------------------------------------------------------

from portail.reply_policy import ReplyPolicy


@pytest.fixture
def policy_no_file(monkeypatch: pytest.MonkeyPatch) -> ReplyPolicy:
    """Return a ReplyPolicy that finds no YAML file on disk (allow-all)."""
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", Path("/nonexistent/reply_policy.yaml"))
    monkeypatch.setattr("portail.reply_policy._DEFAULT_POLICY_PATH", Path("/nonexistent/default.yaml"))
    return ReplyPolicy()


def test_should_reply_returns_true_when_no_config(
    policy_no_file: ReplyPolicy,
) -> None:
    """should_reply() doit retourner True si aucune config n'est chargée."""
    assert policy_no_file.should_reply(make_envelope()) is True


def test_should_reply_returns_false_channel_not_in_whitelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """should_reply() doit retourner False si le channel n'est pas dans la whitelist."""
    policy_file = tmp_path / "reply_policy.yaml"
    policy_file.write_text(
        "enabled: true\nchannels:\n  - telegram\n", encoding="utf-8"
    )
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()

    assert rp.should_reply(make_envelope(channel="discord")) is False


def test_should_reply_returns_false_for_blocked_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """should_reply() doit retourner False si l'utilisateur est bloqué."""
    policy_file = tmp_path / "reply_policy.yaml"
    policy_file.write_text(
        "enabled: true\nblocked_users:\n  - '999'\n", encoding="utf-8"
    )
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()

    blocked_env = make_envelope(sender_id="999")
    allowed_env = make_envelope(sender_id="123")

    assert rp.should_reply(blocked_env) is False
    assert rp.should_reply(allowed_env) is True


def test_enabled_false_blocks_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """should_reply() doit retourner False pour tout envelope quand enabled: false."""
    policy_file = tmp_path / "reply_policy.yaml"
    policy_file.write_text("enabled: false\n", encoding="utf-8")
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()

    assert rp.should_reply(make_envelope(channel="telegram")) is False
    assert rp.should_reply(make_envelope(channel="discord")) is False
    assert rp.should_reply(make_envelope(sender_id="admin")) is False


def test_channel_whitelist_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """should_reply() doit retourner True si le channel est dans la whitelist."""
    policy_file = tmp_path / "reply_policy.yaml"
    policy_file.write_text(
        "enabled: true\nchannels:\n  - telegram\n  - discord\n", encoding="utf-8"
    )
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()

    assert rp.should_reply(make_envelope(channel="telegram")) is True
    assert rp.should_reply(make_envelope(channel="discord")) is True


def test_get_policy_returns_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_policy() doit retourner une copie — modifier le retour n'affecte pas l'état interne."""
    policy_file = tmp_path / "reply_policy.yaml"
    policy_file.write_text("enabled: true\n", encoding="utf-8")
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()

    policy_copy = rp.get_policy()
    policy_copy["enabled"] = False  # Mutate the returned copy

    # Internal state must be unaffected
    assert rp.should_reply(make_envelope()) is True


def test_reload_picks_up_modified_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reload() doit recharger la config modifiée depuis le disque."""
    policy_file = tmp_path / "reply_policy.yaml"
    # Initial config: allow all channels
    policy_file.write_text("enabled: true\n", encoding="utf-8")
    monkeypatch.setattr("portail.reply_policy._USER_POLICY_PATH", policy_file)
    monkeypatch.setattr(
        "portail.reply_policy._DEFAULT_POLICY_PATH",
        Path("/nonexistent/default.yaml"),
    )
    rp = ReplyPolicy()
    assert rp.should_reply(make_envelope(channel="discord")) is True

    # Modify config to restrict channels
    policy_file.write_text(
        "enabled: true\nchannels:\n  - telegram\n", encoding="utf-8"
    )
    rp.reload()

    assert rp.should_reply(make_envelope(channel="discord")) is False
    assert rp.should_reply(make_envelope(channel="telegram")) is True


# ---------------------------------------------------------------------------
# prompt_loader tests
# ---------------------------------------------------------------------------

import portail.prompt_loader as prompt_loader_module
from portail.prompt_loader import load_prompt, list_prompts


@pytest.fixture(autouse=True)
def clear_prompt_cache() -> None:
    """Clear the module-level prompt cache before each test."""
    prompt_loader_module._cache.clear()


def test_load_prompt_returns_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_prompt() doit retourner le contenu du fichier .md s'il existe."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "greeting.md").write_text("Bonjour le monde !", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/prompts")
    )

    result = load_prompt("greeting")
    assert result == "Bonjour le monde !"


def test_load_prompt_returns_empty_string_when_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_prompt() doit retourner "" si le fichier est introuvable."""
    monkeypatch.setattr(
        prompt_loader_module, "_USER_PROMPTS_DIR", Path("/nonexistent/user")
    )
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/repo")
    )

    result = load_prompt("missing_prompt")
    assert result == ""


def test_list_prompts_returns_names_without_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_prompts() doit lister les noms sans extension .md."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "alpha.md").write_text("A", encoding="utf-8")
    (prompts_dir / "beta.md").write_text("B", encoding="utf-8")
    (prompts_dir / "not_a_prompt.txt").write_text("X", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/repo")
    )

    names = list_prompts()
    assert "alpha" in names
    assert "beta" in names
    assert "not_a_prompt" not in names
    # Verify .md is stripped
    assert all(not n.endswith(".md") for n in names)


def test_load_prompt_uses_cache_on_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deux appels successifs ne doivent pas relire le fichier si mtime inchangé."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_file = prompts_dir / "cached.md"
    prompt_file.write_text("Contenu initial", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/repo")
    )

    fixed_mtime = 1_700_000_000.0

    with patch("portail.prompt_loader.os.path.getmtime", return_value=fixed_mtime):
        # First call: cache miss → reads file
        result1 = load_prompt("cached")
        assert result1 == "Contenu initial"

        # Modify the file on disk (simulated) but mtime stays the same
        prompt_file.write_text("Contenu modifié", encoding="utf-8")

        # Second call: cache hit → should NOT re-read
        result2 = load_prompt("cached")

    assert result2 == "Contenu initial"  # Still cached value


def test_load_prompt_from_repo_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_prompt() doit tomber en fallback sur le dossier repo si le fichier n'est pas dans le dossier user."""
    repo_dir = tmp_path / "repo_prompts"
    repo_dir.mkdir()
    (repo_dir / "system.md").write_text("Contenu repo", encoding="utf-8")

    monkeypatch.setattr(
        prompt_loader_module, "_USER_PROMPTS_DIR", Path("/nonexistent/user")
    )
    monkeypatch.setattr(prompt_loader_module, "_REPO_PROMPTS_DIR", repo_dir)

    result = load_prompt("system")
    assert result == "Contenu repo"


def test_load_prompt_user_overrides_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_prompt() doit donner la priorité au dossier user sur le dossier repo."""
    user_dir = tmp_path / "user_prompts"
    user_dir.mkdir()
    repo_dir = tmp_path / "repo_prompts"
    repo_dir.mkdir()

    (user_dir / "greeting.md").write_text("User greeting", encoding="utf-8")
    (repo_dir / "greeting.md").write_text("Repo greeting", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", user_dir)
    monkeypatch.setattr(prompt_loader_module, "_REPO_PROMPTS_DIR", repo_dir)

    result = load_prompt("greeting")
    assert result == "User greeting"


def test_load_prompt_cache_invalidated_on_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_prompt() doit recharger le fichier quand le mtime a changé."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_file = prompts_dir / "dynamic.md"
    prompt_file.write_text("Version 1", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/repo")
    )

    mtime_v1 = 1_700_000_000.0
    mtime_v2 = 1_700_000_001.0  # Simulated mtime bump

    with patch("portail.prompt_loader.os.path.getmtime", return_value=mtime_v1):
        result1 = load_prompt("dynamic")
    assert result1 == "Version 1"

    # Simulate file content change on disk
    prompt_file.write_text("Version 2", encoding="utf-8")

    with patch("portail.prompt_loader.os.path.getmtime", return_value=mtime_v2):
        result2 = load_prompt("dynamic")
    assert result2 == "Version 2"


def test_list_prompts_empty_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_prompts() doit retourner une liste vide si aucun prompt n'existe."""
    empty_user = tmp_path / "empty_user"
    empty_user.mkdir()
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", empty_user)
    monkeypatch.setattr(prompt_loader_module, "_REPO_PROMPTS_DIR", empty_repo)

    assert list_prompts() == []


def test_list_prompts_deduplicates_user_and_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_prompts() doit lister une seule fois un nom présent dans les deux dossiers."""
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    (user_dir / "shared.md").write_text("user", encoding="utf-8")
    (repo_dir / "shared.md").write_text("repo", encoding="utf-8")
    (repo_dir / "repo_only.md").write_text("repo only", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", user_dir)
    monkeypatch.setattr(prompt_loader_module, "_REPO_PROMPTS_DIR", repo_dir)

    names = list_prompts()
    assert names.count("shared") == 1
    assert "repo_only" in names


def test_list_prompts_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_prompts() doit retourner les noms triés alphabétiquement."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    for name in ("zebra", "alpha", "mango"):
        (prompts_dir / f"{name}.md").write_text(".", encoding="utf-8")

    monkeypatch.setattr(prompt_loader_module, "_USER_PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        prompt_loader_module, "_REPO_PROMPTS_DIR", Path("/nonexistent/repo")
    )

    names = list_prompts()
    assert names == sorted(names)
