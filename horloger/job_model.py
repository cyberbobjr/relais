"""HORLOGER job model: JobSpec dataclass and YAML loader.

Each scheduled job is stored as an individual YAML file. This module
provides the immutable ``JobSpec`` dataclass and ``load_job_yaml()``
to parse and validate a single job file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from croniter import croniter as Croniter


# ---------------------------------------------------------------------------
# Required fields — validated explicitly so the error message names them
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "owner_id",
    "schedule",
    "channel",
    "prompt",
    "enabled",
    "created_at",
    "description",
)


# ---------------------------------------------------------------------------
# JobSpec dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    """Immutable representation of a scheduled HORLOGER job.

    Attributes:
        id: Unique job identifier (e.g. ``"weather-morning"``).
        owner_id: Stable user identifier from the portail registry
            (e.g. ``"usr_alice"``).
        schedule: Cron expression (5 fields, standard syntax).
        channel: Target channel name (e.g. ``"discord"``).
        prompt: Natural-language prompt fired when the job triggers.
        enabled: When ``False`` the scheduler skips this job entirely.
        created_at: ISO-8601 creation timestamp string.
        description: Human-readable description of the job purpose.
        timezone: IANA timezone string used to resolve the cron expression.
            Defaults to ``"UTC"``.
    """

    id: str
    owner_id: str
    schedule: str
    channel: str
    prompt: str
    enabled: bool
    created_at: str
    description: str
    timezone: str = "UTC"


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def load_job_yaml(path: Path) -> JobSpec:
    """Parse and validate a job YAML file, returning a frozen ``JobSpec``.

    Reads the YAML file at *path*, checks that all required fields are
    present, and validates the cron expression with ``croniter``.

    Args:
        path: Absolute or relative path to the job YAML file.

    Returns:
        A validated, frozen ``JobSpec`` instance.

    Raises:
        OSError: If *path* cannot be read (includes ``FileNotFoundError``
            when the file does not exist and ``PermissionError`` when access
            is denied).
        ValueError: If a required field is missing or the cron expression
            in ``schedule`` is not valid.
        yaml.YAMLError: If the file content is not valid YAML.
    """
    raw_text = path.read_text(encoding="utf-8")
    data: dict = yaml.safe_load(raw_text) or {}

    # --- Validate required fields ---
    for field in _REQUIRED_FIELDS:
        if field not in data:
            raise ValueError(
                f"Job YAML '{path.name}' is missing required field '{field}'."
            )

    # --- Validate cron expression ---
    schedule: str = data["schedule"]
    if not schedule or not Croniter.is_valid(schedule):
        raise ValueError(
            f"Job YAML '{path.name}' contains an invalid cron schedule: '{schedule}'. "
            "Expected a valid 5-field cron expression (e.g. '0 8 * * *')."
        )

    return JobSpec(
        id=data["id"],
        owner_id=data["owner_id"],
        schedule=schedule,
        channel=data["channel"],
        prompt=data["prompt"],
        enabled=bool(data["enabled"]),
        created_at=data["created_at"],
        description=data["description"],
        timezone=data.get("timezone", "UTC"),
    )
