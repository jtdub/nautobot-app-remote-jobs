"""Lease reaper and wait-timeout enforcement (SPEC 6.2)."""

import logging

from django.db import transaction
from django.utils import timezone
from nautobot.extras.choices import LogLevelChoices

from nautobot_remote_jobs.choices import FailoverPolicyChoices, RunStateChoices
from nautobot_remote_jobs.dispatch.submission import _publish_run_event, log_to_result
from nautobot_remote_jobs.dispatch.tokens import delete_scoped_token
from nautobot_remote_jobs.models import RemoteJobRun

logger = logging.getLogger(__name__)


def reap_expired():
    """Move expired CLAIMED/RUNNING runs to ABANDONED and apply retry policy (SPEC 6.2).

    Also fails PENDING runs stuck past their zone's wait budget when the zone
    policy is 'wait' or 'failover'.
    """
    now = timezone.now()
    reaped = 0
    expired_ids = list(
        RemoteJobRun.objects.filter(
            state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING],
            lease_expires_at__lt=now,
        ).values_list("pk", flat=True)
    )
    for run_id in expired_ids:
        with transaction.atomic():
            run = (
                RemoteJobRun.objects.select_for_update(skip_locked=True)
                .filter(pk=run_id, state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING])
                .first()
            )
            if run is None:
                continue
            abandon_run(run, reason="Lease expired; worker presumed gone.")
            reaped += 1
    reaped += _reap_wait_timeouts(now)
    return reaped


def abandon_run(run, reason):
    """ABANDON a run: revocation bookkeeping, token deletion, retry policy (SPEC 6.2)."""
    log_to_result(run.job_result, reason, level=LogLevelChoices.LOG_WARNING)
    run.transition(RunStateChoices.ABANDONED)
    delete_scoped_token(run)
    _publish_run_event(run, "abandoned")

    definition = run.job_definition
    if run.attempt < definition.retry_max + 1:
        run.attempt += 1
        run.worker = None
        run.lease_expires_at = None
        run.finished_at = None
        run.state = RunStateChoices.PENDING
        run.save(update_fields=["attempt", "worker", "lease_expires_at", "finished_at", "state"])
        run.sync_job_result()
        log_to_result(
            run.job_result,
            f"Re-queued after abandonment (attempt {run.attempt} of {definition.retry_max + 1}).",
        )
        if run.zone:
            from nautobot_remote_jobs.dispatch import notify

            notify.publish_work_available(run.zone)
    else:
        run.refresh_parent_state()


def _reap_wait_timeouts(now):
    """FAILED_DISPATCH for PENDING runs that waited out their zone's max_wait_seconds."""
    failed = 0
    pending = RemoteJobRun.objects.filter(
        state=RunStateChoices.PENDING, zone__isnull=False, queued_at__isnull=False
    ).select_related("zone")
    for run in pending:
        zone = run.zone
        if zone.failover_policy not in (FailoverPolicyChoices.WAIT, FailoverPolicyChoices.FAILOVER):
            continue
        waited = (now - run.queued_at).total_seconds()
        if waited <= zone.max_wait_seconds:
            continue
        with transaction.atomic():
            locked = (
                RemoteJobRun.objects.select_for_update(skip_locked=True)
                .filter(pk=run.pk, state=RunStateChoices.PENDING)
                .first()
            )
            if locked is None:
                continue
            log_to_result(
                locked.job_result,
                f"No capacity in zone '{zone}' within {zone.max_wait_seconds}s wait budget.",
                level=LogLevelChoices.LOG_ERROR,
            )
            locked.state = RunStateChoices.FAILED_DISPATCH
            locked.finished_at = now
            locked.save(update_fields=["state", "finished_at"])
            locked.sync_job_result()
            locked.refresh_parent_state()
            failed += 1
    return failed
