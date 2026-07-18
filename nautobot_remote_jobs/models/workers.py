"""Worker and WorkerEnrollmentToken models (SPEC 4.4-4.5)."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.constants import CHARFIELD_MAX_LENGTH

from nautobot_remote_jobs.choices import WorkerStatusChoices
from nautobot_remote_jobs.constants import DEFAULT_WORKER_TTL_SECONDS


def _app_setting(key, default):
    return settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get(key, default)


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class Worker(PrimaryModel):
    """An enrolled remote worker agent in an execution zone."""

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True, help_text="Set at enrollment.")
    zone = models.ForeignKey(to="nautobot_remote_jobs.ExecutionZone", on_delete=models.PROTECT, related_name="workers")
    enabled = models.BooleanField(default=True, help_text="Admin kill switch. Disabled workers cannot claim.")
    draining = models.BooleanField(default=False, help_text="Set by worker.drain; finishes in-flight, claims nothing.")
    capabilities = models.JSONField(blank=True, default=list)
    capacity = models.PositiveSmallIntegerField(default=4, help_text="Max concurrent runs.")
    agent_version = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    identity_fingerprint = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        help_text="SHA-256 of the worker's session credential.",
    )
    secret_generation = models.PositiveIntegerField(
        default=0,
        help_text="Rotation counter for the derived session secret (worker.rotate).",
    )
    last_seen = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Updated on hello, claim, status, and gateway ping relay (throttled).",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def status(self):
        """ONLINE / DRAINING / OFFLINE per heartbeat TTL (SPEC 4.4)."""
        ttl = _app_setting("worker_ttl_seconds", DEFAULT_WORKER_TTL_SECONDS)
        alive = self.last_seen is not None and self.last_seen >= timezone.now() - timedelta(seconds=ttl)
        if not alive or not self.enabled:
            return WorkerStatusChoices.OFFLINE
        if self.draining:
            return WorkerStatusChoices.DRAINING
        return WorkerStatusChoices.ONLINE

    def touch(self, commit=True):
        """Update last_seen, throttled to at most one write per configured interval."""
        from nautobot_remote_jobs.constants import DEFAULT_LAST_SEEN_THROTTLE_SECONDS

        throttle = _app_setting("last_seen_throttle_seconds", DEFAULT_LAST_SEEN_THROTTLE_SECONDS)
        now = timezone.now()
        if self.last_seen is None or (now - self.last_seen).total_seconds() >= throttle:
            self.last_seen = now
            if commit:
                self.save(update_fields=["last_seen"])
            return True
        return False

    def free_capacity(self):
        """Capacity minus non-terminal claimed/running runs."""
        from nautobot_remote_jobs.choices import RunStateChoices

        in_flight = self.runs.filter(state__in=[RunStateChoices.CLAIMED, RunStateChoices.RUNNING]).count()
        return max(0, self.capacity - in_flight)

    @staticmethod
    def fingerprint(session_secret):
        """SHA-256 fingerprint of a session secret."""
        return hashlib.sha256(session_secret.encode()).hexdigest()


class WorkerEnrollmentToken(BaseModel):
    """Single-purpose bootstrap credential; grants nothing except the enroll exchange (SPEC 4.5)."""

    token_hash = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        unique=True,
        help_text="SHA-256 of the token. Plaintext shown once at creation, never stored.",
    )
    zone = models.ForeignKey(
        to="nautobot_remote_jobs.ExecutionZone",
        on_delete=models.CASCADE,
        related_name="enrollment_tokens",
    )
    expires = models.DateTimeField(help_text="Default: 24 hours after creation.")
    single_use = models.BooleanField(default=True)
    used_at = models.DateTimeField(blank=True, null=True)
    worker = models.ForeignKey(
        to=Worker, on_delete=models.SET_NULL, related_name="enrollment_tokens", blank=True, null=True
    )
    created_by = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["-expires"]
        verbose_name = "Worker Enrollment Token"
        verbose_name_plural = "Worker Enrollment Tokens"

    def __str__(self):
        return f"Enrollment token for {self.zone} (expires {self.expires:%Y-%m-%d %H:%M})"

    @classmethod
    def generate(cls, zone, created_by=None, ttl_hours=24, single_use=True):
        """Create a token; returns (instance, plaintext). Plaintext is never stored."""
        plaintext = secrets.token_urlsafe(32)
        instance = cls.objects.create(
            token_hash=cls.hash_token(plaintext),
            zone=zone,
            expires=timezone.now() + timedelta(hours=ttl_hours),
            single_use=single_use,
            created_by=created_by,
        )
        return instance, plaintext

    @staticmethod
    def hash_token(plaintext):
        """SHA-256 hex digest of the plaintext token."""
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @property
    def is_valid(self):
        """Usable: not expired and not already consumed (when single-use)."""
        if timezone.now() >= self.expires:
            return False
        if self.single_use and self.used_at is not None:
            return False
        return True
