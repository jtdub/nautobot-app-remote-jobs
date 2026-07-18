"""RemoteJobSchedule model (SPEC 4.7)."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from nautobot.apps.models import PrimaryModel, extras_features
from nautobot.core.constants import CHARFIELD_MAX_LENGTH

from nautobot_remote_jobs.choices import ScheduleIntervalChoices
from nautobot_remote_jobs.models._compat import ApprovableModelMixin

INTERVAL_DELTAS = {
    ScheduleIntervalChoices.HOURLY: timedelta(hours=1),
    ScheduleIntervalChoices.DAILY: timedelta(days=1),
    ScheduleIntervalChoices.WEEKLY: timedelta(weeks=1),
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
class RemoteJobSchedule(ApprovableModelMixin, PrimaryModel):
    """A recurring or one-shot schedule that enqueues RemoteJobRun rows via the beat task."""

    job_definition = models.ForeignKey(
        to="nautobot_remote_jobs.JobDefinition", on_delete=models.CASCADE, related_name="schedules"
    )
    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    enabled = models.BooleanField(default=True)
    interval = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=ScheduleIntervalChoices,
        default=ScheduleIntervalChoices.ONCE,
    )
    crontab = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        blank=True,
        help_text="Standard 5-field cron expression; required for 'custom' interval.",
    )
    start_time = models.DateTimeField()
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Runs execute as this user (token scoping).",
    )
    inputs = models.JSONField(blank=True, default=dict)
    last_run_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Remote Job Schedule"
        verbose_name_plural = "Remote Job Schedules"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.interval == ScheduleIntervalChoices.CUSTOM:
            if not self.crontab:
                raise ValidationError({"crontab": "Required for custom interval."})
            fields = self.crontab.split()
            if len(fields) != 5:
                raise ValidationError({"crontab": "Must be a standard 5-field cron expression."})

    def next_run_after(self, reference):
        """Next due datetime strictly after `reference`, or None when nothing is due anymore.

        The dispatcher beat task calls this every 60s; minute-level resolution suffices.
        """
        if self.interval == ScheduleIntervalChoices.ONCE:
            if self.last_run_at is not None:
                return None
            return self.start_time
        if self.interval == ScheduleIntervalChoices.CUSTOM:
            try:
                from croniter import croniter

                # First fire is anchored at start_time, not at "now": anchoring
                # to max(now, start_time) would always push the next occurrence
                # into the future, so a schedule whose start_time is already past
                # (the normal case) would never become due and never fire.
                base = self.last_run_at if self.last_run_at is not None else self.start_time
                return croniter(self.crontab, base).get_next(ret_type=type(reference))
            except ImportError:  # pragma: no cover - croniter is an install requirement
                return None
        delta = INTERVAL_DELTAS[self.interval]
        anchor = self.last_run_at or self.start_time
        if self.last_run_at is None:
            return self.start_time
        return anchor + delta

    def is_due(self, now):
        """True when a run should be enqueued at `now`."""
        if not self.enabled:
            return False
        if now < self.start_time:
            return False
        next_run = self.next_run_after(now)
        if self.interval == ScheduleIntervalChoices.ONCE:
            return self.last_run_at is None and next_run is not None and next_run <= now
        if next_run is None:
            return False
        return next_run <= now
