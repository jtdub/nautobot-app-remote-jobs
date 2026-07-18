"""Claim protocol, lease renewal, and completion (SPEC 6.2, 8.1)."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from nautobot_remote_jobs.choices import RunStateChoices
from nautobot_remote_jobs.constants import (
    DEFAULT_LEASE_SECONDS,
    RPC_ILLEGAL_TRANSITION,
    RPC_LEASE_EXPIRED,
    RPC_UNKNOWN_RUN,
)
from nautobot_remote_jobs.dispatch.submission import _publish_run_event, log_to_result
from nautobot_remote_jobs.dispatch.tokens import delete_scoped_token, mint_scoped_token
from nautobot_remote_jobs.models import JobDefinition, RemoteJobRun

logger = logging.getLogger(__name__)


class ClaimError(Exception):
    """Structured dispatch error carrying a JSON-RPC error code (SPEC 8.3)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def lease_seconds():
    """Configured lease duration."""
    return settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get("lease_seconds", DEFAULT_LEASE_SECONDS)


def claim_runs(worker, max_offers=1):
    """Claim up to max_offers PENDING runs for this worker (SPEC 6.2).

    Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent claimers never contend.
    Returns a list of JobOffer dicts (SPEC 8.1); tokens are minted inside the
    claim transaction and delivered only in the RPC result.
    """
    if not worker.enabled or worker.draining:
        return []
    offers = []
    budget = min(max_offers, worker.free_capacity())
    worker_capabilities = set(worker.capabilities or [])
    while len(offers) < budget:
        offer = _claim_one(worker, worker_capabilities)
        if offer is None:
            break
        offers.append(offer)
    return offers


def _claim_one(worker, worker_capabilities):
    with transaction.atomic():
        candidates = (
            RemoteJobRun.objects.select_for_update(skip_locked=True)
            .filter(state=RunStateChoices.PENDING, zone=worker.zone)
            .order_by("queued_at")
        )
        run = None
        held_singletons = set()  # definition ids already locked+found held this pass
        for candidate in candidates[:20]:
            required = set(candidate.job_definition.capabilities or [])
            if required and not required.issubset(worker_capabilities):
                continue
            if candidate.job_definition.singleton:
                definition_id = candidate.job_definition_id
                if definition_id in held_singletons:
                    continue  # already determined held this pass; don't re-lock/re-query
                # Serialize concurrent claims of the same singleton definition by
                # locking its row: a peer claiming a *different* PENDING run of
                # this definition blocks here until we commit, so it then sees our
                # CLAIMED run and skips (SPEC 4.1/6.2). Locking only pre-existing
                # CLAIMED/RUNNING rows is not enough — two workers can each pick a
                # different PENDING run and neither sees the other's uncommitted
                # transition (write-skew).
                JobDefinition.objects.select_for_update().only("pk").get(pk=definition_id)
                if _singleton_held(candidate):
                    # Stays PENDING behind the running one (SPEC 6.2).
                    held_singletons.add(definition_id)
                    continue
            run = candidate
            break
        if run is None:
            return None
        now = timezone.now()
        run.worker = worker
        run.claimed_at = now
        run.lease_expires_at = now + timedelta(seconds=lease_seconds())
        run.state = RunStateChoices.CLAIMED
        run.save(update_fields=["worker", "claimed_at", "lease_expires_at", "state"])
        run.sync_job_result()
        token = mint_scoped_token(run)
        offer = build_job_offer(run, token)
    _publish_run_event(run, "claimed")
    worker.touch()
    return offer


def _singleton_held(run):
    return (
        RemoteJobRun.objects.select_for_update()
        .filter(
            job_definition=run.job_definition,
            state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING],
        )
        .exclude(pk=run.pk)
        .exists()
    )


def build_job_offer(run, token):
    """The JobOffer payload delivered in the job.claim RPC result (SPEC 8.1)."""
    definition = run.job_definition
    nautobot_url = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get(
        "nautobot_url", getattr(settings, "SANITIZED_URL", "") or ""
    )
    return {
        "run_id": str(run.pk),
        "definition": definition.name,
        "image": definition.image_with_digest,
        "inputs": run.inputs,
        "input_schema": definition.input_schema or {},
        "timeout_seconds": definition.timeout_seconds,
        "grace_seconds": definition.grace_seconds,
        "nautobot_url": nautobot_url,
        "token": token.key,
        "secrets_groups": [group.name for group in definition.secrets_groups.all()],
        "env": {
            "REMOTE_JOBS_RUN_ID": str(run.pk),
            "REMOTE_JOBS_ZONE": run.zone.name if run.zone else "",
            "REMOTE_JOBS_DRYRUN": "true" if run.dryrun else "false",
        },
    }


def _get_worker_run(worker, run_id):
    try:
        run = RemoteJobRun.objects.select_related("job_definition", "job_result", "zone").get(pk=run_id)
    except (RemoteJobRun.DoesNotExist, ValueError) as exc:
        raise ClaimError(RPC_UNKNOWN_RUN, f"Unknown run {run_id}") from exc
    if run.worker_id != worker.pk:
        raise ClaimError(RPC_UNKNOWN_RUN, f"Run {run_id} is not claimed by this worker")
    return run


def report_status(worker, run_id, state, progress=None):
    """Handle job.status: doubles as lease renewal and cancel poll (SPEC 8.1)."""
    with transaction.atomic():
        run = _get_worker_run(worker, run_id)
        run = RemoteJobRun.objects.select_for_update().get(pk=run.pk)
        if run.state not in (RunStateChoices.CLAIMED, RunStateChoices.RUNNING):
            raise ClaimError(
                RPC_LEASE_EXPIRED if run.state == RunStateChoices.ABANDONED else RPC_ILLEGAL_TRANSITION,
                f"Run {run_id} is {run.state}",
            )
        started = False
        if state == RunStateChoices.RUNNING and run.state == RunStateChoices.CLAIMED:
            run.transition(RunStateChoices.RUNNING)
            started = True
        run.lease_expires_at = timezone.now() + timedelta(seconds=lease_seconds())
        run.save(update_fields=["lease_expires_at"])
        if progress and progress.get("message"):
            log_to_result(
                run.job_result,
                f"Progress {progress.get('current', '?')}/{progress.get('total', '?')}: {progress['message']}",
                grouping="progress",
            )
        cancel_requested = bool(run.job_result.celery_kwargs.get("cancel_requested"))
    if started:
        _publish_run_event(run, "started")
    worker.touch()
    return {
        "lease_expires_at": run.lease_expires_at.isoformat(),
        "cancel_requested": cancel_requested,
    }


def complete_run(worker, run_id, state, exit_code=None, image_digest_executed="", error=None):
    """Handle job.complete. Idempotent per run_id (SPEC 8.1)."""
    if state not in (RunStateChoices.SUCCESS, RunStateChoices.FAILURE, RunStateChoices.TERMINATED):
        raise ClaimError(RPC_ILLEGAL_TRANSITION, f"Invalid completion state {state}")
    with transaction.atomic():
        run = _get_worker_run(worker, run_id)
        run = RemoteJobRun.objects.select_for_update().get(pk=run.pk)
        if run.state == state:
            return {"ok": True}  # idempotent replay
        if run.state not in (RunStateChoices.CLAIMED, RunStateChoices.RUNNING):
            raise ClaimError(RPC_ILLEGAL_TRANSITION, f"Run {run_id} is already {run.state}")
        if run.state == RunStateChoices.CLAIMED and state != RunStateChoices.TERMINATED:
            # Worker may complete a fast job without an interleaved RUNNING status.
            run.transition(RunStateChoices.RUNNING)
        if image_digest_executed:
            run.image_digest_executed = image_digest_executed
            run.save(update_fields=["image_digest_executed"])
        if error:
            log_to_result(run.job_result, f"Worker reported error: {error}", level="error")
        if exit_code is not None:
            log_to_result(run.job_result, f"Container exit code: {exit_code}", grouping="post_run")
        run.transition(state)
        delete_scoped_token(run)
        run.refresh_parent_state()
    event = "terminated" if state == RunStateChoices.TERMINATED else "completed"
    _publish_run_event(run, event)
    worker.touch()
    return {"ok": True}


def reconcile_in_flight(worker, run_ids):
    """worker.hello reconciliation: runs the agent still tracks survive; the rest get reaped early.

    Any CLAIMED/RUNNING run assigned to this worker that the agent no longer
    tracks is moved to ABANDONED immediately instead of waiting for lease expiry.
    """
    from nautobot_remote_jobs.dispatch.reaper import abandon_run

    known = {str(run_id) for run_id in run_ids}
    stale = RemoteJobRun.objects.filter(
        worker=worker, state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING]
    ).exclude(pk__in=known)
    for run in stale:
        abandon_run(run, reason="Worker restarted without this run in its journal.")
    # Extend leases for runs the agent does still track.
    RemoteJobRun.objects.filter(
        worker=worker,
        state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING],
        pk__in=known,
    ).update(lease_expires_at=timezone.now() + timedelta(seconds=lease_seconds()))


def hello_result(worker):
    """The worker.hello RPC result (SPEC 8.1)."""
    config = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {})
    return {
        "server_time": timezone.now().isoformat(),
        "draining": worker.draining,
        "lease_seconds": lease_seconds(),
        "log_sink_config": config.get("log_sink_config", {"type": "http"}),
    }
