"""Remote cancel strategy for the core CancelFactory (SPEC 11).

CancelFactory.strategies is not a documented extension point; registration is
guarded in RemoteJobsConfig.ready(). When core does not expose the factory
(e.g. Nautobot 3.2.0b1), the app's own Cancel button on the run detail view
drives cancel_run() directly.
"""

import logging

from django.utils import timezone

from nautobot_remote_jobs.choices import CancelModeChoices, RunStateChoices, WorkerStatusChoices

logger = logging.getLogger(__name__)


def cancel_run(run, user=None, mode=CancelModeChoices.GRACEFUL):
    """Cancel a run in any non-terminal state.

    - Fan-out/per_device parent (has children, worker=None): cancel every
      non-terminal child and mark the parent TERMINATED. The parent is an
      aggregator, not a worker-backed run, so it must not be reaped/retried.
    - PENDING/OFFERED: mark CANCELLED immediately (no worker involvement).
    - CLAIMED/RUNNING with a live worker: publish job.cancel; the worker sends
      job.complete(state=TERMINATED). Fall through to reap is handled by the
      lease reaper if the worker never responds.
    - CLAIMED/RUNNING with an offline worker: reap immediately (ABANDONED).
    Returns a short human-readable outcome string.
    """
    from django.db import transaction

    from nautobot_remote_jobs.dispatch import notify
    from nautobot_remote_jobs.dispatch.reaper import abandon_run
    from nautobot_remote_jobs.dispatch.submission import log_to_result
    from nautobot_remote_jobs.dispatch.tokens import delete_scoped_token
    from nautobot_remote_jobs.models import RemoteJobRun

    is_parent = False
    child_ids_to_cancel = []
    worker_to_notify = None
    with transaction.atomic():
        # No select_related here: FOR UPDATE cannot touch the nullable side of
        # an outer join on PostgreSQL.
        run = RemoteJobRun.objects.select_for_update().get(pk=run.pk)
        if run.is_terminal:
            return f"Run is already {run.state}."

        # Parent (fan-out/per_device) aggregator: worker is None but this is not
        # a "worker gone" case. Cancel the children (each on its own worker) and
        # terminate the parent directly; never abandon_run it, which would
        # re-queue a zone-less, unclaimable zombie under any retry policy.
        children = list(run.children.all())
        is_parent = bool(children)
        child_ids_to_cancel = [child.pk for child in children if not child.is_terminal]
        if is_parent:
            log_to_result(
                run.job_result,
                f"Cancelled by {user or 'system'}; signalling {len(child_ids_to_cancel)} child run(s).",
            )
            run.state = RunStateChoices.TERMINATED
            run.finished_at = timezone.now()
            run.save(update_fields=["state", "finished_at"])
            delete_scoped_token(run)
            run.sync_job_result(revoked_by=user if user is not None else None)

        elif run.state in (RunStateChoices.PENDING, RunStateChoices.OFFERED):
            log_to_result(run.job_result, f"Cancelled by {user or 'system'} before claim.")
            run.transition(RunStateChoices.CANCELLED)
            delete_scoped_token(run)
            _record_revoked_by(run, user)
            run.refresh_parent_state()
            return "Cancelled before any worker involvement."

        else:
            worker = run.worker
            if worker is None or worker.status == WorkerStatusChoices.OFFLINE:
                abandon_run(run, reason=f"Cancel requested by {user or 'system'}; worker offline, reaped.")
                _record_revoked_by(run, user)
                return "Worker offline; run reaped (ABANDONED)."

            # Terminate path: flag cancel for the job.status poll fallback and push job.cancel.
            job_result = run.job_result
            celery_kwargs = dict(job_result.celery_kwargs or {})
            celery_kwargs["cancel_requested"] = True
            celery_kwargs["cancel_requested_at"] = timezone.now().isoformat()
            job_result.celery_kwargs = celery_kwargs
            job_result.save(update_fields=["celery_kwargs"])
            _record_revoked_by(run, user)
            log_to_result(run.job_result, f"Terminate requested by {user or 'system'} (mode={mode}).")
            worker_to_notify = worker

    if worker_to_notify is not None:
        notify.publish_cancel(worker_to_notify, run, mode=mode)
        return f"job.cancel ({mode}) sent to worker {worker_to_notify.name}."

    # Parent path only: cancel each non-terminal child on its own worker.
    if is_parent:
        _cancel_children(child_ids_to_cancel, user, mode)
        return f"Cancelled parent run; signalled {len(child_ids_to_cancel)} child run(s)."

    return f"Run is {run.state}."


def _cancel_children(child_ids, user, mode):
    """Cancel each still-present child run (fan-out/per_device cancel)."""
    from nautobot_remote_jobs.models import RemoteJobRun

    for child_id in child_ids:
        try:
            child = RemoteJobRun.objects.get(pk=child_id)
        except RemoteJobRun.DoesNotExist:  # pragma: no cover - concurrent completion
            continue
        cancel_run(child, user=user, mode=mode)


def _record_revoked_by(run, user):
    if user is None:
        return
    job_result = run.job_result
    update_fields = []
    if hasattr(job_result, "revoked_by"):
        job_result.revoked_by = user
        update_fields.append("revoked_by")
    if hasattr(job_result, "revoked_by_user_name"):
        job_result.revoked_by_user_name = getattr(user, "username", str(user))
        update_fields.append("revoked_by_user_name")
    if update_fields:
        job_result.save(update_fields=update_fields)


def build_remote_cancel_strategy():
    """Build the strategy class against whatever core interface is available.

    Returns None when the core factory interface cannot be found; the caller
    logs and the app relies on its own Cancel button (SPEC 11 fallback).
    """
    try:
        from nautobot.extras.jobs_cancel import CancelStrategy  # type: ignore[attr-defined]
    except ImportError:
        try:
            from nautobot.extras.jobs_cancel import BaseStrategy as CancelStrategy  # type: ignore[attr-defined]
        except ImportError:
            return None

    class RemoteCancelStrategy(CancelStrategy):  # pylint: disable=too-few-public-methods
        """Terminate/reap strategy for remote runs (SPEC 11)."""

        def terminate(self, job_result, user=None, **kwargs):
            run = getattr(job_result, "remote_job_run", None)
            if run is None:
                logger.warning("JobResult %s has no remote run; nothing to terminate", job_result.pk)
                return
            cancel_run(run, user=user, mode=CancelModeChoices.GRACEFUL)

        def reap(self, job_result, user=None, **kwargs):
            from nautobot_remote_jobs.dispatch.reaper import abandon_run

            run = getattr(job_result, "remote_job_run", None)
            if run is None or run.is_terminal:
                return
            abandon_run(run, reason=f"Reaped via core cancel by {user or 'system'}.")

        def is_alive(self, job_result, **kwargs):
            run = getattr(job_result, "remote_job_run", None)
            if run is None or run.worker is None:
                return False
            return run.worker.status != WorkerStatusChoices.OFFLINE

    return RemoteCancelStrategy


def register_cancel_strategy():
    """Register the remote strategy with core CancelFactory; guarded (SPEC 11)."""
    from nautobot_remote_jobs.constants import REMOTE_QUEUE_TYPE

    try:
        from nautobot.extras.jobs_cancel import CancelFactory  # type: ignore[attr-defined]
    except ImportError:
        logger.warning(
            "nautobot.extras.jobs_cancel.CancelFactory not available in this Nautobot version; "
            "falling back to the app's own Cancel action on run detail views."
        )
        return False
    strategy = build_remote_cancel_strategy()
    if strategy is None or not isinstance(getattr(CancelFactory, "strategies", None), dict):
        logger.warning("CancelFactory interface changed; falling back to the app's own Cancel action.")
        return False
    CancelFactory.strategies[REMOTE_QUEUE_TYPE] = strategy
    logger.info("Registered remote cancel strategy with core CancelFactory.")
    return True
