"""Custom exceptions for the RELAIS bundle system.

All bundle-related errors derive from ``BundleError`` for easy catch-all
handling. ``BundleConflictWarning`` is a ``UserWarning`` subclass — it is
issued via ``warnings.warn`` rather than raised, so it never interrupts
the install pipeline.
"""

from __future__ import annotations


class BundleError(Exception):
    """Base class for all bundle-related errors."""


class BundleValidationError(BundleError):
    """Raised when a bundle ZIP or its manifest fails validation.

    Covers: not-a-ZIP, ZIP bomb, path traversal, missing bundle.yaml,
    invalid manifest name, root-dir/name mismatch.
    """


class BundleInstallError(BundleError):
    """Raised when a filesystem write fails during bundle installation.

    Examples: permission denied, staging rename fails.
    """


class BundleNotFoundError(BundleError):
    """Raised when the requested bundle is not installed.

    Raised by ``uninstall_bundle`` when ``bundles_dir/<name>`` does not exist.
    """


class BundleConflictWarning(UserWarning):
    """Issued when a tool name in the new bundle clashes with an existing bundle.

    This is a warning, not an error — installation proceeds but the conflict
    is logged so operators can investigate.
    """
