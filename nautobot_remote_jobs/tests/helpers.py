"""Shared test object factories."""

from django.contrib.auth import get_user_model
from django.utils import timezone
from nautobot.extras.choices import JobResultStatusChoices
from nautobot.extras.models import JobResult

from nautobot_remote_jobs.models import ExecutionZone, JobDefinition, RemoteJobRun, Worker

User = get_user_model()

DIGEST = "sha256:" + "a" * 64


def make_user(username="runner"):
    return User.objects.get_or_create(username=username)[0]


def make_zone(name="dfw-dc1", **kwargs):
    return ExecutionZone.objects.create(name=name, **kwargs)


def make_definition(name="rotate-admin", zone=None, **kwargs):
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("image", "registry.example.com/jobs/rotate-admin:1.4.0")
    kwargs.setdefault("image_digest", DIGEST)
    if zone is not None:
        kwargs.setdefault("default_zone", zone)
    return JobDefinition.objects.create(name=name, **kwargs)


def make_worker(zone, name="worker-1", online=True, **kwargs):
    kwargs.setdefault("capacity", 4)
    worker = Worker.objects.create(
        name=name,
        zone=zone,
        identity_fingerprint="f" * 64,
        last_seen=timezone.now() if online else None,
        **kwargs,
    )
    return worker


def make_run(definition, user=None, zone=None, **kwargs):
    user = user or make_user()
    job_result = JobResult.objects.create(
        name=f"[remote] {definition.name}",
        user=user,
        status=JobResultStatusChoices.STATUS_PENDING,
    )
    kwargs.setdefault("queued_at", timezone.now())
    return RemoteJobRun.objects.create(
        job_definition=definition,
        job_result=job_result,
        zone=zone or definition.default_zone,
        **kwargs,
    )
