"""Per-job scoped token minting and cleanup (SPEC 7.3)."""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from nautobot.users.models import Token

from nautobot_remote_jobs.constants import DEFAULT_TOKEN_EXTRA_TTL_SECONDS

logger = logging.getLogger(__name__)


def mint_scoped_token(run):
    """Mint a users.Token owned by the launching user, bounded by the run's wall clock budget.

    Because token auth acts as the user, everything the job does is constrained
    by the launcher's ObjectPermissions (SPEC 7.3).
    """
    definition = run.job_definition
    extra = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get(
        "token_extra_ttl_seconds", DEFAULT_TOKEN_EXTRA_TTL_SECONDS
    )
    ttl = definition.timeout_seconds + definition.grace_seconds + extra
    token = Token.objects.create(
        user=run.job_result.user,
        expires=timezone.now() + timedelta(seconds=ttl),
        write_enabled=True,
        description=f"remote-jobs run {run.pk}",
    )
    run.scoped_token = token
    run.save(update_fields=["scoped_token"])
    return token


def delete_scoped_token(run):
    """Delete the run's scoped token; called on every terminal transition and by the reaper."""
    token = run.scoped_token
    if token is None:
        return
    run.scoped_token = None
    run.save(update_fields=["scoped_token"])
    try:
        token.delete()
    except Exception:  # noqa: BLE001 - cleanup must not mask the terminal transition
        logger.warning("Failed to delete scoped token for run %s", run.pk, exc_info=True)
