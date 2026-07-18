"""Human-facing REST API viewsets (SPEC 5).

Standard NautobotModelViewSet CRUD honoring ObjectPermissions, plus
POST /job-definitions/{id}/run/.
"""

from nautobot.apps.api import NautobotModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from nautobot_remote_jobs import filters, models
from nautobot_remote_jobs.api import serializers
from nautobot_remote_jobs.cancel import cancel_run
from nautobot_remote_jobs.choices import CancelModeChoices
from nautobot_remote_jobs.dispatch.submission import SubmissionError, submit_run


class JobDefinitionViewSet(NautobotModelViewSet):
    """CRUD for JobDefinition plus the run action."""

    queryset = models.JobDefinition.objects.all()
    serializer_class = serializers.JobDefinitionSerializer
    filterset_class = filters.JobDefinitionFilterSet

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, pk=None):
        """Submit a run of this definition as the requesting user (SPEC 5)."""
        definition = self.get_object()
        body = serializers.RunRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            run = submit_run(
                definition=definition,
                user=request.user,
                inputs=body.validated_data.get("inputs") or {},
                dryrun=body.validated_data.get("dryrun", False),
            )
        except SubmissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            serializers.RemoteJobRunSerializer(run, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ExecutionZoneViewSet(NautobotModelViewSet):
    """CRUD for ExecutionZone."""

    queryset = models.ExecutionZone.objects.all()
    serializer_class = serializers.ExecutionZoneSerializer
    filterset_class = filters.ExecutionZoneFilterSet


class ZoneMembershipRuleViewSet(NautobotModelViewSet):
    """CRUD for ZoneMembershipRule."""

    queryset = models.ZoneMembershipRule.objects.all()
    serializer_class = serializers.ZoneMembershipRuleSerializer
    filterset_class = filters.ZoneMembershipRuleFilterSet


class WorkerViewSet(NautobotModelViewSet):
    """CRUD for Worker plus drain action."""

    queryset = models.Worker.objects.all()
    serializer_class = serializers.WorkerSerializer
    filterset_class = filters.WorkerFilterSet

    @action(detail=True, methods=["post"], url_path="drain")
    def drain(self, request, pk=None):
        """Set draining and push worker.drain to the agent (SPEC 8.2)."""
        from nautobot_remote_jobs.dispatch import notify

        worker = self.get_object()
        worker.draining = True
        worker.save(update_fields=["draining"])
        notify.publish_drain(worker)
        return Response({"status": "draining"})


class WorkerEnrollmentTokenViewSet(NautobotModelViewSet):
    """Create/list/revoke enrollment tokens. Plaintext appears only in the create response."""

    queryset = models.WorkerEnrollmentToken.objects.all()
    serializer_class = serializers.WorkerEnrollmentTokenSerializer
    filterset_class = filters.WorkerEnrollmentTokenFilterSet
    http_method_names = ["get", "post", "delete", "head", "options"]


class RemoteJobRunViewSet(NautobotModelViewSet):
    """Read runs; cancel via action. Runs are created through submission, not POST."""

    queryset = models.RemoteJobRun.objects.select_related("job_definition", "zone", "worker", "job_result")
    serializer_class = serializers.RemoteJobRunSerializer
    filterset_class = filters.RemoteJobRunFilterSet
    http_method_names = ["get", "post", "delete", "head", "options"]

    def create(self, request, *args, **kwargs):
        return Response(
            {"detail": "Runs are created via POST /job-definitions/{id}/run/."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Cancel this run (app-native path; core Cancel button uses the strategy)."""
        run = self.get_object()
        mode = request.data.get("mode", CancelModeChoices.GRACEFUL)
        outcome = cancel_run(run, user=request.user, mode=mode)
        return Response({"detail": outcome})


class RunArtifactViewSet(NautobotModelViewSet):
    """Read-only artifact metadata for humans."""

    queryset = models.RunArtifact.objects.select_related("run")
    serializer_class = serializers.RunArtifactSerializer
    filterset_class = filters.RunArtifactFilterSet
    http_method_names = ["get", "head", "options"]


class RemoteJobScheduleViewSet(NautobotModelViewSet):
    """CRUD for RemoteJobSchedule."""

    queryset = models.RemoteJobSchedule.objects.select_related("job_definition", "user")
    serializer_class = serializers.RemoteJobScheduleSerializer
    filterset_class = filters.RemoteJobScheduleFilterSet
