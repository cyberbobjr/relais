"""Backward-compatibility shim — profile_loader lives in common/.

All symbols are re-exported from ``common.profile_loader``.
Import directly from ``common.profile_loader`` in new code.
"""

from common.profile_loader import (  # noqa: F401
    ProfileConfig,
    ResilienceConfig,
    build_chat_model,
    load_profiles,
    resolve_profile,
)
