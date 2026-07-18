"""FilterSets for nautobot_remote_jobs."""

import django_filters
from nautobot.apps.filters import BaseFilterSet, NautobotFilterSet, SearchFilter

from nautobot_remote_jobs import models


class JobDefinitionFilterSet(NautobotFilterSet):
    """Filters for JobDefinition."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains", "image": "icontains"})

    class Meta:
        model = models.JobDefinition
        fields = [
            "enabled",
            "zone_policy",
            "default_zone",
            "singleton",
            "dryrun_supported",
            "requires_zone_local",
            "tags",
        ]


class ExecutionZoneFilterSet(NautobotFilterSet):
    """Filters for ExecutionZone."""

    q = SearchFilter(filter_predicates={"name": "icontains", "description": "icontains"})

    class Meta:
        model = models.ExecutionZone
        fields = ["enabled", "failover_policy", "priority", "tags"]


class ZoneMembershipRuleFilterSet(BaseFilterSet):
    """Filters for ZoneMembershipRule."""

    q = SearchFilter(filter_predicates={"zone__name": "icontains"})

    class Meta:
        model = models.ZoneMembershipRule
        fields = ["zone", "locations", "roles", "prefixes", "dynamic_group"]


class WorkerFilterSet(NautobotFilterSet):
    """Filters for Worker."""

    q = SearchFilter(filter_predicates={"name": "icontains", "agent_version": "icontains"})

    class Meta:
        model = models.Worker
        fields = ["zone", "enabled", "draining", "tags"]


class WorkerEnrollmentTokenFilterSet(BaseFilterSet):
    """Filters for WorkerEnrollmentToken."""

    q = SearchFilter(filter_predicates={"zone__name": "icontains"})

    class Meta:
        model = models.WorkerEnrollmentToken
        fields = ["zone", "single_use", "worker"]


class RemoteJobRunFilterSet(NautobotFilterSet):
    """Filters for RemoteJobRun (mirrors Job Results filters + zone/worker/attempt, SPEC 15)."""

    q = SearchFilter(filter_predicates={"job_definition__name": "icontains"})
    has_parent = django_filters.BooleanFilter(field_name="parent", lookup_expr="isnull", exclude=True)

    class Meta:
        model = models.RemoteJobRun
        fields = ["job_definition", "state", "zone", "worker", "device", "dryrun", "attempt", "parent", "tags"]


class RunArtifactFilterSet(BaseFilterSet):
    """Filters for RunArtifact."""

    q = SearchFilter(filter_predicates={"name": "icontains"})

    class Meta:
        model = models.RunArtifact
        fields = ["run", "uploaded"]


class RemoteJobScheduleFilterSet(NautobotFilterSet):
    """Filters for RemoteJobSchedule."""

    q = SearchFilter(filter_predicates={"name": "icontains", "job_definition__name": "icontains"})

    class Meta:
        model = models.RemoteJobSchedule
        fields = ["job_definition", "enabled", "interval", "user", "tags"]
