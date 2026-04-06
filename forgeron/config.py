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

    min_traces_before_analysis: int = 5
    min_error_rate: float = 0.3
    min_improvement_interval_seconds: int = 3600
    rollback_error_rate_threshold: float = 0.2
    rollback_window_traces: int = 3
    llm_profile: str = "precise"
    annotation_profile: str = "fast"
    annotation_mode: bool = True
    patch_mode: bool = True
    annotation_min_tool_errors: int = 1
    annotation_cooldown_seconds: int = 300
    skills_dir: Path | None = None
    # --- Création automatique de skills (Solution D) ---
    creation_mode: bool = True
    """Enable automatic skill creation from recurring session patterns."""
    min_sessions_for_creation: int = 3
    """Minimum number of sessions with the same intent_label before creating a skill."""
    creation_cooldown_seconds: int = 86400
    """Minimum interval between two creation attempts for the same intent label (seconds).
    Redis TTL key: relais:skill:creation_cooldown:{intent_label}"""
    max_sessions_for_labeling: int = 5
    """Maximum number of representative sessions to pass to SkillCreator."""
    notify_user_on_patch: bool = True
    """Publish a notification to relais:messages:outgoing_pending when a patch is applied."""
    notify_user_on_creation: bool = True
    """Publish a notification to relais:messages:outgoing_pending when a skill is created."""

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
        min_traces_before_analysis=int(
            section.get("min_traces_before_analysis", 5)
        ),
        min_error_rate=float(section.get("min_error_rate", 0.3)),
        min_improvement_interval_seconds=int(
            section.get("min_improvement_interval_seconds", 3600)
        ),
        rollback_error_rate_threshold=float(
            section.get("rollback_error_rate_threshold", 0.2)
        ),
        rollback_window_traces=int(section.get("rollback_window_traces", 3)),
        llm_profile=str(section.get("llm_profile", "precise")),
        annotation_profile=str(section.get("annotation_profile", "fast")),
        annotation_mode=bool(section.get("annotation_mode", True)),
        patch_mode=bool(section.get("patch_mode", True)),
        annotation_min_tool_errors=int(
            section.get("annotation_min_tool_errors", 1)
        ),
        annotation_cooldown_seconds=int(
            section.get("annotation_cooldown_seconds", 300)
        ),
        skills_dir=skills_dir,
        creation_mode=bool(section.get("creation_mode", True)),
        min_sessions_for_creation=int(section.get("min_sessions_for_creation", 3)),
        creation_cooldown_seconds=int(section.get("creation_cooldown_seconds", 86400)),
        max_sessions_for_labeling=int(section.get("max_sessions_for_labeling", 5)),
        notify_user_on_patch=bool(section.get("notify_user_on_patch", True)),
        notify_user_on_creation=bool(section.get("notify_user_on_creation", True)),
    )
