"""Tables for nautobot_remote_jobs."""

import django_tables2 as tables
from nautobot.apps.tables import BaseTable, BooleanColumn, ButtonsColumn, TagColumn, ToggleColumn

from nautobot_remote_jobs import models

WORKER_STATUS_BADGE = """
{% if record.status == "online" %}<span class="badge text-bg-success">Online</span>
{% elif record.status == "draining" %}<span class="badge text-bg-warning">Draining</span>
{% else %}<span class="badge text-bg-danger">Offline</span>{% endif %}
"""

ZONE_WORKERS = """{{ record.online_worker_count }} online / {{ record.worker_count }} total"""

RUN_STATE_BADGE = """
{% if record.state == "SUCCESS" %}<span class="badge text-bg-success">{{ record.state }}</span>
{% elif record.state == "FAILURE" or record.state == "FAILED_DISPATCH" %}<span class="badge text-bg-danger">{{ record.state }}</span>
{% elif record.state == "RUNNING" %}<span class="badge text-bg-info">{{ record.state }}</span>
{% elif record.state == "TERMINATED" or record.state == "ABANDONED" or record.state == "CANCELLED" %}<span class="badge text-bg-warning">{{ record.state }}</span>
{% else %}<span class="badge text-bg-secondary">{{ record.state }}</span>{% endif %}
"""


class JobDefinitionTable(BaseTable):
    """JobDefinition list table."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    enabled = BooleanColumn()
    default_zone = tables.Column(linkify=True)
    singleton = BooleanColumn()
    dryrun_supported = BooleanColumn()
    tags = TagColumn(url_name="plugins:nautobot_remote_jobs:jobdefinition_list")
    actions = ButtonsColumn(models.JobDefinition)

    class Meta(BaseTable.Meta):
        model = models.JobDefinition
        fields = (
            "pk",
            "name",
            "enabled",
            "image",
            "zone_policy",
            "default_zone",
            "timeout_seconds",
            "singleton",
            "dryrun_supported",
            "tags",
            "actions",
        )
        default_columns = ("pk", "name", "enabled", "image", "zone_policy", "default_zone", "actions")


class ExecutionZoneTable(BaseTable):
    """ExecutionZone list table with live worker counts (SPEC 4.2)."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    enabled = BooleanColumn()
    workers = tables.TemplateColumn(ZONE_WORKERS, verbose_name="Workers", orderable=False)
    actions = ButtonsColumn(models.ExecutionZone)

    class Meta(BaseTable.Meta):
        model = models.ExecutionZone
        fields = ("pk", "name", "enabled", "priority", "failover_policy", "max_wait_seconds", "workers", "actions")
        default_columns = ("pk", "name", "enabled", "priority", "failover_policy", "workers", "actions")


class ZoneMembershipRuleTable(BaseTable):
    """ZoneMembershipRule list table."""

    pk = ToggleColumn()
    zone = tables.Column(linkify=True)
    dynamic_group = tables.Column(linkify=True)
    actions = ButtonsColumn(models.ZoneMembershipRule)

    class Meta(BaseTable.Meta):
        model = models.ZoneMembershipRule
        fields = ("pk", "zone", "include_descendant_locations", "dynamic_group", "weight", "actions")


class WorkerTable(BaseTable):
    """Worker list table with status badges (SPEC 15)."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    zone = tables.Column(linkify=True)
    status = tables.TemplateColumn(WORKER_STATUS_BADGE, orderable=False)
    enabled = BooleanColumn()
    draining = BooleanColumn()
    actions = ButtonsColumn(models.Worker)

    class Meta(BaseTable.Meta):
        model = models.Worker
        fields = (
            "pk",
            "name",
            "zone",
            "status",
            "enabled",
            "draining",
            "capacity",
            "agent_version",
            "last_seen",
            "actions",
        )
        default_columns = ("pk", "name", "zone", "status", "capacity", "agent_version", "last_seen", "actions")


class WorkerEnrollmentTokenTable(BaseTable):
    """Enrollment token list table."""

    pk = ToggleColumn()
    zone = tables.Column(linkify=True)
    worker = tables.Column(linkify=True)
    single_use = BooleanColumn()

    class Meta(BaseTable.Meta):
        model = models.WorkerEnrollmentToken
        fields = ("pk", "zone", "expires", "single_use", "used_at", "worker", "created_by")


class RemoteJobRunTable(BaseTable):
    """Run list table mirroring Job Results columns + zone/worker/attempt (SPEC 15)."""

    pk = ToggleColumn()
    id = tables.Column(linkify=True, verbose_name="Run")
    job_definition = tables.Column(linkify=True)
    state = tables.TemplateColumn(RUN_STATE_BADGE)
    zone = tables.Column(linkify=True)
    worker = tables.Column(linkify=True)
    device = tables.Column(linkify=True)
    job_result = tables.Column(linkify=True, verbose_name="Job Result")
    dryrun = BooleanColumn()

    class Meta(BaseTable.Meta):
        model = models.RemoteJobRun
        fields = (
            "pk",
            "id",
            "job_definition",
            "state",
            "zone",
            "worker",
            "device",
            "attempt",
            "dryrun",
            "queued_at",
            "started_at",
            "finished_at",
            "job_result",
        )
        default_columns = (
            "pk",
            "id",
            "job_definition",
            "state",
            "zone",
            "worker",
            "attempt",
            "queued_at",
            "finished_at",
        )


class RunArtifactTable(BaseTable):
    """Artifact list table."""

    pk = ToggleColumn()
    name = tables.Column()
    run = tables.Column(linkify=True)
    uploaded = BooleanColumn()

    class Meta(BaseTable.Meta):
        model = models.RunArtifact
        fields = ("pk", "name", "run", "content_type", "size_bytes", "sha256", "uploaded")


class RemoteJobScheduleTable(BaseTable):
    """Schedule list table."""

    pk = ToggleColumn()
    name = tables.Column(linkify=True)
    job_definition = tables.Column(linkify=True)
    enabled = BooleanColumn()
    actions = ButtonsColumn(models.RemoteJobSchedule)

    class Meta(BaseTable.Meta):
        model = models.RemoteJobSchedule
        fields = (
            "pk",
            "name",
            "job_definition",
            "enabled",
            "interval",
            "start_time",
            "user",
            "last_run_at",
            "actions",
        )
