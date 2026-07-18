"""UI views for nautobot_remote_jobs (SPEC 15)."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View
from nautobot.apps.views import NautobotUIViewSet
from nautobot.core.views.mixins import ObjectPermissionRequiredMixin

from nautobot_remote_jobs import filters, forms, models, tables
from nautobot_remote_jobs.api import serializers
from nautobot_remote_jobs.cancel import cancel_run
from nautobot_remote_jobs.choices import CancelModeChoices
from nautobot_remote_jobs.dispatch.submission import SubmissionError, submit_run
from nautobot_remote_jobs.dispatch.zones import zone_coverage_report


class JobDefinitionUIViewSet(NautobotUIViewSet):
    """Job Definitions: list/detail/edit + Run button (SPEC 15)."""

    queryset = models.JobDefinition.objects.all()
    filterset_class = filters.JobDefinitionFilterSet
    filterset_form_class = forms.JobDefinitionFilterForm
    form_class = forms.JobDefinitionForm
    bulk_update_form_class = forms.JobDefinitionBulkEditForm
    serializer_class = serializers.JobDefinitionSerializer
    table_class = tables.JobDefinitionTable


class ExecutionZoneUIViewSet(NautobotUIViewSet):
    """Execution Zones: list/detail with rules, worker counts, failover chain (SPEC 15)."""

    queryset = models.ExecutionZone.objects.all()
    filterset_class = filters.ExecutionZoneFilterSet
    filterset_form_class = forms.ExecutionZoneFilterForm
    form_class = forms.ExecutionZoneForm
    bulk_update_form_class = forms.ExecutionZoneBulkEditForm
    serializer_class = serializers.ExecutionZoneSerializer
    table_class = tables.ExecutionZoneTable

    def get_extra_context(self, request, instance=None):
        context = super().get_extra_context(request, instance)
        if instance is not None:
            context["membership_rules"] = instance.membership_rules.all()
            context["workers_table"] = tables.WorkerTable(instance.workers.all(), orderable=False)
            context["failover_chain"] = instance.ordered_failover_zones
        return context


class ZoneMembershipRuleUIViewSet(NautobotUIViewSet):
    """Zone membership rule CRUD."""

    queryset = models.ZoneMembershipRule.objects.select_related("zone", "dynamic_group")
    filterset_class = filters.ZoneMembershipRuleFilterSet
    form_class = forms.ZoneMembershipRuleForm
    serializer_class = serializers.ZoneMembershipRuleSerializer
    table_class = tables.ZoneMembershipRuleTable


class WorkerUIViewSet(NautobotUIViewSet):
    """Workers: list with status badges, drain/disable actions (SPEC 15)."""

    queryset = models.Worker.objects.select_related("zone")
    filterset_class = filters.WorkerFilterSet
    filterset_form_class = forms.WorkerFilterForm
    form_class = forms.WorkerForm
    serializer_class = serializers.WorkerSerializer
    table_class = tables.WorkerTable

    def get_extra_context(self, request, instance=None):
        context = super().get_extra_context(request, instance)
        if instance is not None:
            context["runs_table"] = tables.RemoteJobRunTable(
                instance.runs.select_related("job_definition", "zone")[:25], orderable=False
            )
        return context


class WorkerEnrollmentTokenUIViewSet(NautobotUIViewSet):
    """Enrollment tokens: create (plaintext shown once), list, delete."""

    queryset = models.WorkerEnrollmentToken.objects.select_related("zone", "worker")
    filterset_class = filters.WorkerEnrollmentTokenFilterSet
    form_class = forms.WorkerEnrollmentTokenForm
    serializer_class = serializers.WorkerEnrollmentTokenSerializer
    table_class = tables.WorkerEnrollmentTokenTable

    def form_save(self, form, **kwargs):
        """Surface the one-time plaintext via a message after generation."""
        instance = super().form_save(form, **kwargs)
        plaintext = getattr(instance, "plaintext_token", None)
        if plaintext:
            messages.warning(
                self.request,
                f"Enrollment token (shown once, copy it now): {plaintext}",
            )
        return instance


class RemoteJobRunUIViewSet(NautobotUIViewSet):
    """Runs: list mirrors Job Results; detail links to the core JobResult (SPEC 15)."""

    queryset = models.RemoteJobRun.objects.select_related(
        "job_definition", "zone", "worker", "device", "job_result", "parent"
    )
    filterset_class = filters.RemoteJobRunFilterSet
    filterset_form_class = forms.RemoteJobRunFilterForm
    serializer_class = serializers.RemoteJobRunSerializer
    table_class = tables.RemoteJobRunTable
    action_buttons = ()

    def get_extra_context(self, request, instance=None):
        context = super().get_extra_context(request, instance)
        if instance is not None:
            context["children_table"] = tables.RemoteJobRunTable(
                instance.children.select_related("job_definition", "zone", "worker"),
                orderable=False,
            )
            context["artifacts_table"] = tables.RunArtifactTable(instance.artifacts.all(), orderable=False)
        return context


class RemoteJobScheduleUIViewSet(NautobotUIViewSet):
    """Schedules: approval state surfaces via the core approval workflow UI (SPEC 15)."""

    queryset = models.RemoteJobSchedule.objects.select_related("job_definition", "user")
    filterset_class = filters.RemoteJobScheduleFilterSet
    filterset_form_class = forms.RemoteJobScheduleFilterForm
    form_class = forms.RemoteJobScheduleForm
    serializer_class = serializers.RemoteJobScheduleSerializer
    table_class = tables.RemoteJobScheduleTable


class JobDefinitionRunView(ObjectPermissionRequiredMixin, View):
    """GET: dynamic form rendered from input_schema; POST: submit the run (SPEC 14, 15)."""

    queryset = models.JobDefinition.objects.all()

    def get_required_permission(self):
        return "nautobot_remote_jobs.add_remotejobrun"

    def get(self, request, pk):
        definition = get_object_or_404(models.JobDefinition.objects.restrict(request.user, "view"), pk=pk)
        form = forms.build_run_form(definition)
        return render(
            request,
            "nautobot_remote_jobs/jobdefinition_run.html",
            {"object": definition, "form": form},
        )

    def post(self, request, pk):
        definition = get_object_or_404(models.JobDefinition.objects.restrict(request.user, "view"), pk=pk)
        form = forms.build_run_form(definition, data=request.POST)
        if not form.is_valid():
            return render(
                request,
                "nautobot_remote_jobs/jobdefinition_run.html",
                {"object": definition, "form": form},
            )
        inputs, dryrun = forms.run_form_to_inputs(definition, form)
        try:
            run = submit_run(definition=definition, user=request.user, inputs=inputs, dryrun=dryrun)
        except SubmissionError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "nautobot_remote_jobs/jobdefinition_run.html",
                {"object": definition, "form": form},
            )
        messages.success(request, f"Run {run.pk} submitted.")
        return redirect("plugins:nautobot_remote_jobs:remotejobrun", pk=run.pk)


class RemoteJobRunCancelView(ObjectPermissionRequiredMixin, View):
    """App-native Cancel button on the run detail view (SPEC 11 fallback)."""

    queryset = models.RemoteJobRun.objects.all()

    def get_required_permission(self):
        return "nautobot_remote_jobs.change_remotejobrun"

    def post(self, request, pk):
        run = get_object_or_404(models.RemoteJobRun.objects.restrict(request.user, "change"), pk=pk)
        mode = request.POST.get("mode", CancelModeChoices.GRACEFUL)
        outcome = cancel_run(run, user=request.user, mode=mode)
        messages.info(request, outcome)
        return redirect("plugins:nautobot_remote_jobs:remotejobrun", pk=run.pk)


class ZoneCoverageReportView(ObjectPermissionRequiredMixin, View):
    """Zone Coverage report (SPEC 4.3, 15)."""

    queryset = models.ExecutionZone.objects.all()

    def get_required_permission(self):
        return "nautobot_remote_jobs.view_executionzone"

    def get(self, request):
        from nautobot.dcim.models import Device

        devices = Device.objects.select_related("location", "role").all()
        report = zone_coverage_report(devices)
        return render(
            request,
            "nautobot_remote_jobs/zone_coverage.html",
            {
                "uncovered": report["uncovered"],
                "ambiguous": report["ambiguous"],
                "empty_zones": report["empty_zones"],
                "device_count": devices.count(),
            },
        )
