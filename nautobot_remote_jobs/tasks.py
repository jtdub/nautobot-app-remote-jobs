"""Celery tasks (SPEC 4.7, 6.2).

Discovered by Nautobot's Celery app via autodiscover_tasks(). Beat entries are
registered in RemoteJobsConfig.ready().
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="remote_jobs.dispatch_scheduled")
def dispatch_scheduled():
    """Enqueue RemoteJobRun rows for due schedules (SPEC 4.7). Never talks to workers."""
    from django.utils import timezone

    from nautobot_remote_jobs.dispatch.submission import SubmissionError, submit_run
    from nautobot_remote_jobs.models import RemoteJobSchedule

    now = timezone.now()
    dispatched = 0
    for schedule in RemoteJobSchedule.objects.filter(enabled=True).select_related("job_definition", "user"):
        if not schedule.is_due(now):
            continue
        try:
            submit_run(
                definition=schedule.job_definition,
                user=schedule.user,
                inputs=schedule.inputs or {},
            )
            dispatched += 1
        except SubmissionError as exc:
            logger.warning("Schedule %s could not submit: %s", schedule.name, exc)
        finally:
            schedule.last_run_at = now
            schedule.save(update_fields=["last_run_at"])
    return dispatched


@shared_task(name="remote_jobs.reap_expired")
def reap_expired():
    """Reap expired leases and wait timeouts (SPEC 6.2)."""
    from nautobot_remote_jobs.dispatch.reaper import reap_expired as _reap

    return _reap()
