"""RemoteJobRun and RunArtifact models (SPEC 4.6, 4.8)."""

from django.db import models
from django.utils import timezone
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.constants import CHARFIELD_MAX_LENGTH
from nautobot.extras.choices import JobResultStatusChoices, JobRevocationTypeChoices

from nautobot_remote_jobs.choices import RunStateChoices
from nautobot_remote_jobs.models._compat import ApprovableModelMixin

# RemoteJobRun.state -> core JobResult.status (SPEC 6.3).
STATE_TO_JOB_RESULT_STATUS = {
    RunStateChoices.PENDING: JobResultStatusChoices.STATUS_PENDING,
    RunStateChoices.OFFERED: JobResultStatusChoices.STATUS_PENDING,
    RunStateChoices.CLAIMED: JobResultStatusChoices.STATUS_PENDING,
    RunStateChoices.RUNNING: JobResultStatusChoices.STATUS_STARTED,
    RunStateChoices.SUCCESS: JobResultStatusChoices.STATUS_SUCCESS,
    RunStateChoices.FAILURE: JobResultStatusChoices.STATUS_FAILURE,
    RunStateChoices.TERMINATED: JobResultStatusChoices.STATUS_REVOKED,
    RunStateChoices.ABANDONED: JobResultStatusChoices.STATUS_REVOKED,
    RunStateChoices.CANCELLED: JobResultStatusChoices.STATUS_FAILURE,
    RunStateChoices.FAILED_DISPATCH: JobResultStatusChoices.STATUS_FAILURE,
}

# Legal state transitions (SPEC 6.1).
LEGAL_TRANSITIONS = {
    RunStateChoices.PENDING: {
        RunStateChoices.OFFERED,
        RunStateChoices.CLAIMED,  # pull-path claims skip OFFERED
        RunStateChoices.CANCELLED,
        RunStateChoices.FAILED_DISPATCH,
    },
    RunStateChoices.OFFERED: {RunStateChoices.CLAIMED, RunStateChoices.PENDING, RunStateChoices.CANCELLED},
    RunStateChoices.CLAIMED: {
        RunStateChoices.RUNNING,
        RunStateChoices.PENDING,
        RunStateChoices.ABANDONED,
        RunStateChoices.TERMINATED,
    },
    RunStateChoices.RUNNING: {
        RunStateChoices.SUCCESS,
        RunStateChoices.FAILURE,
        RunStateChoices.TERMINATED,
        RunStateChoices.ABANDONED,
    },
    RunStateChoices.ABANDONED: {RunStateChoices.PENDING},  # retry policy only
}


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class RemoteJobRun(ApprovableModelMixin, PrimaryModel):
    """One execution attempt unit; the dispatch-side record backing a core JobResult."""

    job_definition = models.ForeignKey(
        to="nautobot_remote_jobs.JobDefinition", on_delete=models.PROTECT, related_name="runs"
    )
    job_result = models.OneToOneField(to="extras.JobResult", on_delete=models.PROTECT, related_name="remote_job_run")
    parent = models.ForeignKey(to="self", on_delete=models.CASCADE, related_name="children", blank=True, null=True)
    zone = models.ForeignKey(
        to="nautobot_remote_jobs.ExecutionZone",
        on_delete=models.PROTECT,
        related_name="runs",
        blank=True,
        null=True,
        help_text="Resolved zone. Null until resolution for per_device.",
    )
    worker = models.ForeignKey(
        to="nautobot_remote_jobs.Worker",
        on_delete=models.SET_NULL,
        related_name="runs",
        blank=True,
        null=True,
        help_text="Set at claim.",
    )
    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.SET_NULL,
        related_name="remote_job_runs",
        blank=True,
        null=True,
        help_text="For per-device child runs.",
    )
    state = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=RunStateChoices,
        default=RunStateChoices.PENDING,
        db_index=True,
    )
    inputs = models.JSONField(
        blank=True,
        default=dict,
        help_text='Validated against input_schema. writeOnly keys stored as "__redacted__".',
    )
    dryrun = models.BooleanField(default=False)
    lease_expires_at = models.DateTimeField(blank=True, null=True, db_index=True)
    attempt = models.PositiveSmallIntegerField(default=1)
    image_digest_executed = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        blank=True,
        help_text="Reported by the worker in job.complete. Provenance.",
    )
    scoped_token = models.ForeignKey(
        to="users.Token",
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
        help_text="Deleted on terminal state.",
    )
    queued_at = models.DateTimeField(blank=True, null=True)
    offered_at = models.DateTimeField(blank=True, null=True)
    claimed_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    is_saved_view_model = False

    class Meta:
        ordering = ["-queued_at"]
        verbose_name = "Remote Job Run"
        verbose_name_plural = "Remote Job Runs"
        indexes = [
            models.Index(fields=["state", "zone", "queued_at"]),
        ]

    def __str__(self):
        return f"{self.job_definition.name} ({self.state})"

    @property
    def is_terminal(self):
        """True when no further state transitions are expected (barring ABANDONED retry)."""
        return self.state in RunStateChoices.TERMINAL_STATES or self.state == RunStateChoices.ABANDONED

    def can_transition_to(self, new_state):
        """Whether the state machine permits moving to new_state."""
        return new_state in LEGAL_TRANSITIONS.get(self.state, set())

    def transition(self, new_state, save=True, **timestamps):
        """Apply a state transition and mirror it into the linked JobResult (SPEC 4.6 sync rule).

        Callers are expected to wrap this in a transaction with the row locked.
        Raises ValueError on an illegal transition.
        """
        if new_state == self.state:
            return
        if not self.can_transition_to(new_state):
            raise ValueError(f"Illegal transition {self.state} -> {new_state}")
        self.state = new_state
        now = timezone.now()
        auto_stamps = {
            RunStateChoices.OFFERED: "offered_at",
            RunStateChoices.CLAIMED: "claimed_at",
            RunStateChoices.RUNNING: "started_at",
        }
        update_fields = ["state"]
        if new_state in auto_stamps and getattr(self, auto_stamps[new_state]) is None:
            setattr(self, auto_stamps[new_state], now)
            update_fields.append(auto_stamps[new_state])
        if new_state in RunStateChoices.TERMINAL_STATES or new_state == RunStateChoices.ABANDONED:
            self.finished_at = now
            update_fields.append("finished_at")
        for field, value in timestamps.items():
            setattr(self, field, value)
            update_fields.append(field)
        if save:
            self.save(update_fields=update_fields)
        self.sync_job_result()

    def sync_job_result(self, revoked_by=None):
        """Mirror state into the core JobResult per the SPEC 6.3 mapping."""
        job_result = self.job_result
        job_result.status = STATE_TO_JOB_RESULT_STATUS[self.state]
        update_fields = ["status"]
        if self.worker and job_result.worker != self.worker.name:
            job_result.worker = self.worker.name
            update_fields.append("worker")
        if self.state == RunStateChoices.RUNNING and job_result.date_started is None:
            job_result.date_started = self.started_at or timezone.now()
            update_fields.append("date_started")
        if self.state in (
            RunStateChoices.SUCCESS,
            RunStateChoices.FAILURE,
            RunStateChoices.CANCELLED,
            RunStateChoices.FAILED_DISPATCH,
        ):
            job_result.date_done = self.finished_at or timezone.now()
            update_fields.append("date_done")
        if self.state in (RunStateChoices.TERMINATED, RunStateChoices.ABANDONED):
            revocation_type = (
                JobRevocationTypeChoices.TYPE_TERMINATED
                if self.state == RunStateChoices.TERMINATED
                else JobRevocationTypeChoices.TYPE_ABANDONED
            )
            job_result.revocation_type = revocation_type
            update_fields.append("revocation_type")
            if revoked_by is not None:
                job_result.revoked_by = revoked_by
                update_fields.append("revoked_by")
            # Field name differs between 3.2 betas ("date_revoked") and the
            # next-branch spec ("terminated_at"); set whichever exists.
            for field_name in ("date_revoked", "terminated_at"):
                if hasattr(job_result, field_name):
                    setattr(job_result, field_name, self.finished_at or timezone.now())
                    update_fields.append(field_name)
                    break
            job_result.date_done = self.finished_at or timezone.now()
            update_fields.append("date_done")
        job_result.save(update_fields=update_fields)

    def refresh_parent_state(self):
        """Derive a parent run's state from its children (SPEC 6.4)."""
        parent = self.parent
        if parent is None:
            return
        child_states = list(parent.children.values_list("state", flat=True))
        if not child_states:
            return
        terminal_like = set(RunStateChoices.TERMINAL_STATES) | {RunStateChoices.ABANDONED}
        if not all(state in terminal_like for state in child_states):
            return  # parent stays non-terminal until all children are terminal
        if all(state == RunStateChoices.SUCCESS for state in child_states):
            new_state = RunStateChoices.SUCCESS
        else:
            new_state = RunStateChoices.FAILURE
        if parent.state in (
            RunStateChoices.PENDING,
            RunStateChoices.RUNNING,
            RunStateChoices.CLAIMED,
            RunStateChoices.OFFERED,
        ):
            # Parent aggregates only; force the mapping directly.
            parent.state = new_state
            parent.finished_at = timezone.now()
            parent.save(update_fields=["state", "finished_at"])
            parent.sync_job_result()


class RunArtifact(BaseModel):
    """A file produced by a run, stored via the configured Django storage backend (SPEC 4.8)."""

    run = models.ForeignKey(to=RemoteJobRun, on_delete=models.CASCADE, related_name="artifacts")
    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH)
    content_type = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    storage_path = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded = models.BooleanField(default=False, help_text="Set when the upload is completed.")

    class Meta:
        ordering = ["run", "name"]
        unique_together = [["run", "name"]]
        verbose_name = "Run Artifact"
        verbose_name_plural = "Run Artifacts"

    def __str__(self):
        return f"{self.name} ({self.run_id})"
