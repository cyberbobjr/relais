"""HORLOGER job registry: scans a directory of YAML job files.

``JobRegistry`` provides a simple in-memory catalogue of ``JobSpec``
instances loaded from ``*.yaml`` files in a configured directory. It
supports initial loading and hot-reload without restarting the process.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from horloger.job_model import JobSpec, load_job_yaml

logger = logging.getLogger(__name__)


class JobRegistry:
    """In-memory registry of scheduled jobs loaded from YAML files.

    The registry scans *jobs_dir* for ``*.yaml`` files and parses each
    with :func:`~horloger.job_model.load_job_yaml`. Files that fail
    parsing or validation are skipped with a WARNING log — they never
    block the load of other jobs.

    Attributes:
        jobs_dir: Directory containing ``{id}.yaml`` job files.
    """

    def __init__(self, jobs_dir: Path) -> None:
        """Initialise the registry.

        Args:
            jobs_dir: Path to the directory containing job YAML files.
        """
        self.jobs_dir: Path = jobs_dir
        self._jobs: dict[str, JobSpec] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_all(self) -> dict[str, JobSpec]:
        """Scan *jobs_dir*, parse every ``*.yaml`` file, and return the result.

        Files that raise any exception (missing fields, invalid cron,
        malformed YAML, etc.) are skipped and a WARNING is logged for each.
        The internal state is updated to reflect only the successfully loaded
        jobs from this scan.

        Returns:
            A dict mapping ``job_id`` to the corresponding :class:`~horloger.job_model.JobSpec`.
            The dict is keyed by the ``id`` field inside the YAML, *not* the
            file stem.
        """
        loaded: dict[str, JobSpec] = {}

        for yaml_path in sorted(self.jobs_dir.glob("*.yaml")):
            try:
                spec = load_job_yaml(yaml_path)
                loaded[spec.id] = spec
            except (ValueError, yaml.YAMLError, OSError) as exc:
                logger.warning(
                    "Skipping job file '%s': %s",
                    yaml_path.name,
                    exc,
                )

        self._jobs = loaded
        return dict(self._jobs)

    def reload(self) -> dict[str, JobSpec]:
        """Re-scan the directory and refresh the internal state.

        Equivalent to calling :meth:`load_all` again. Newly added files are
        picked up; deleted or now-invalid files are dropped.

        Returns:
            A dict mapping ``job_id`` to :class:`~horloger.job_model.JobSpec`
            reflecting the current state of *jobs_dir*.
        """
        return self.load_all()

    def get(self, job_id: str) -> JobSpec | None:
        """Return the :class:`~horloger.job_model.JobSpec` for *job_id*, or ``None``.

        Args:
            job_id: The ``id`` field of the desired job.

        Returns:
            The matching :class:`~horloger.job_model.JobSpec` if found,
            ``None`` otherwise (including before :meth:`load_all` has been
            called).
        """
        return self._jobs.get(job_id)
