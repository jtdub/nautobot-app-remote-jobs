"""JobDefinition model (SPEC 4.1)."""

import re

import jsonschema
from django.core.exceptions import ValidationError
from django.db import models
from nautobot.apps.models import PrimaryModel, extras_features
from nautobot.core.constants import CHARFIELD_MAX_LENGTH

from nautobot_remote_jobs.choices import RunStateChoices, ZonePolicyChoices
from nautobot_remote_jobs.constants import IMAGE_DIGEST_PATTERN


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class JobDefinition(PrimaryModel):
    """A remotely executable job: an OCI image plus dispatch metadata."""

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    enabled = models.BooleanField(
        default=False,
        help_text="Disabled definitions are not runnable, matching core Job semantics.",
    )
    image = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text="OCI reference without digest, e.g. registry.example.com/jobs/rotate-admin:1.4.0",
    )
    image_digest = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text="sha256:... digest. Dispatch always sends image@digest.",
    )
    input_schema = models.JSONField(
        blank=True,
        default=dict,
        help_text="JSON Schema (draft 2020-12) describing job inputs; drives form rendering and validation.",
    )
    zone_policy = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=ZonePolicyChoices,
        default=ZonePolicyChoices.PINNED,
    )
    default_zone = models.ForeignKey(
        to="nautobot_remote_jobs.ExecutionZone",
        on_delete=models.PROTECT,
        related_name="pinned_job_definitions",
        blank=True,
        null=True,
        help_text="Required when zone_policy is 'pinned'.",
    )
    secrets_groups = models.ManyToManyField(
        to="extras.SecretsGroup",
        related_name="remote_job_definitions",
        blank=True,
        help_text="Secrets groups the job may resolve.",
    )
    capabilities = models.JSONField(
        blank=True,
        default=list,
        help_text='Labels a worker must advertise to claim, e.g. ["ssh-access"].',
    )
    timeout_seconds = models.PositiveIntegerField(default=1800, help_text="Hard wall clock limit.")
    grace_seconds = models.PositiveIntegerField(default=30, help_text="SIGTERM to SIGKILL window on cancel/timeout.")
    retry_max = models.PositiveSmallIntegerField(
        default=0,
        help_text="Automatic requeue count for ABANDONED runs only. FAILURE never auto-retries.",
    )
    singleton = models.BooleanField(
        default=False, help_text="At most one non-terminal run of this definition at a time."
    )
    dryrun_supported = models.BooleanField(default=False)
    requires_zone_local = models.BooleanField(default=True, help_text="When set, failover to another zone is refused.")

    class Meta:
        ordering = ["name"]
        verbose_name = "Job Definition"
        verbose_name_plural = "Job Definitions"

    def __str__(self):
        return self.name

    @property
    def image_with_digest(self):
        """Full pinned OCI reference sent to workers."""
        base = self.image.rsplit(":", 1)[0] if ":" in self.image.rsplit("/", 1)[-1] else self.image
        return f"{base}@{self.image_digest}"

    def clean(self):
        """Validate digest format, input schema, and zone policy coherence (SPEC 4.1)."""
        super().clean()
        errors = {}
        if self.image_digest and not re.match(IMAGE_DIGEST_PATTERN, self.image_digest):
            errors["image_digest"] = "Must match sha256:<64 hex characters>."
        if self.input_schema:
            if not isinstance(self.input_schema, dict):
                errors["input_schema"] = "Must be a JSON object."
            else:
                try:
                    jsonschema.Draft202012Validator.check_schema(self.input_schema)
                except jsonschema.SchemaError as exc:
                    errors["input_schema"] = f"Invalid JSON Schema: {exc.message}"
        if self.zone_policy == ZonePolicyChoices.PINNED and not self.default_zone:
            errors["default_zone"] = "Required when zone_policy is 'pinned'."
        if not isinstance(self.capabilities, list):
            errors["capabilities"] = "Must be a list of capability labels."
        if errors:
            raise ValidationError(errors)

    def has_active_runs(self):
        """True if any non-terminal run of this definition exists (singleton support)."""
        return self.runs.filter(state__in=RunStateChoices.NON_TERMINAL_STATES).exists()
