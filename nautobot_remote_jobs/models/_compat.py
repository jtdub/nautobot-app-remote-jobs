"""Compatibility shims for Nautobot version differences.

The app targets Nautobot >= 3.2 (SPEC section 1). These guarded imports let the
models module load against earlier versions in development environments; the
fallbacks are no-ops and MUST NOT be relied upon in production deployments.
"""

from django.db import models

try:
    # Nautobot 3.x generic approvals framework (SPEC section 2 / 4.6 / 4.7).
    # Verified location in 3.2.0b1: nautobot.extras.models.mixins.
    from nautobot.extras.models.mixins import ApprovableModelMixin
except ImportError:  # pragma: no cover - fallback for pre-3.2 dev environments

    class ApprovableModelMixin(models.Model):
        """No-op stand-in when the core approvals framework is unavailable."""

        class Meta:
            abstract = True


__all__ = ("ApprovableModelMixin",)
