"""Configuration loader for the Forgeron brick."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common.config_loader import resolve_config_path, resolve_skills_dir


@dataclass
class ForgeonConfig:
    """Runtime configuration for the Forgeron brick.

    All thresholds and toggles are loaded from ``forgeron.yaml`` via the
    standard config cascade (user > system > project).
    """

    llm_profile: str = "precise"
    edit_profile: str = "precise"
    edit_mode: bool = True
    edit_min_tool_errors: int = 1
    edit_cooldown_seconds: int = 300
    edit_call_threshold: int = 5
    """Edit a skill after this many cumulative calls, even without errors."""
    skills_dir: Path | None = None
    # --- Automatic skill creation from session archives ---
    creation_mode: bool = True
    """Enable automatic skill creation from recurring session patterns."""
    min_sessions_for_creation: int = 3
    """Minimum number of sessions with the same intent_label before creating a skill."""
    creation_cooldown_seconds: int = 86400
    """Minimum interval between two creation attempts for the same intent label (seconds).
    Redis TTL key: relais:skill:creation_cooldown:{intent_label}"""
    max_sessions_for_labeling: int = 5
    """Maximum number of representative sessions to pass to SkillCreator."""
    notify_user_on_creation: bool = True
    """Publish a notification to relais:messages:outgoing_pending when a skill is created."""
    # --- Correction pipeline (user feedback → skill fix via skill-designer) ---
    correction_mode: bool = True
    """Enable the correction pipeline: user feedback triggers skill-designer via force_subagent."""
    history_read_timeout_seconds: int = 30
    """Seconds to wait for Souvenir BRPOP response when fetching session history."""

    def __post_init__(self) -> None:
        if self.skills_dir is None:
            self.skills_dir = resolve_skills_dir()


def load_forgeron_config() -> ForgeonConfig:
    """Load Forgeron config from the YAML cascade.

    Returns:
        A ``ForgeonConfig`` with values from ``forgeron.yaml`` merged over
        the defaults.  Missing keys fall back to dataclass defaults.
    """
    try:
        config_path: Path = resolve_config_path("forgeron.yaml")
    except FileNotFoundError:
        return ForgeonConfig()

    with config_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    section: dict = raw.get("forgeron", {})

    skills_dir_raw = section.get("skills_dir")
    skills_dir: Path | None = Path(skills_dir_raw) if skills_dir_raw else None

    return ForgeonConfig(
        llm_profile=str(section.get("llm_profile", "precise")),
        edit_profile=str(section.get("edit_profile", "precise")),
        edit_mode=bool(section.get("edit_mode", True)),
        edit_min_tool_errors=int(section.get("edit_min_tool_errors", 1)),
        edit_cooldown_seconds=int(section.get("edit_cooldown_seconds", 300)),
        edit_call_threshold=int(section.get("edit_call_threshold", 5)),
        skills_dir=skills_dir,
        creation_mode=bool(section.get("creation_mode", True)),
        min_sessions_for_creation=int(section.get("min_sessions_for_creation", 3)),
        creation_cooldown_seconds=int(section.get("creation_cooldown_seconds", 86400)),
        max_sessions_for_labeling=int(section.get("max_sessions_for_labeling", 5)),
        notify_user_on_creation=bool(section.get("notify_user_on_creation", True)),
        correction_mode=bool(section.get("correction_mode", True)),
        history_read_timeout_seconds=int(section.get("history_read_timeout_seconds", 30)),
    )
