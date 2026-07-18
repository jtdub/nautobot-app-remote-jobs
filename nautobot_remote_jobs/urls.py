"""Django urlpatterns declaration for nautobot_remote_jobs app."""

from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView
from nautobot.apps.urls import NautobotUIViewSetRouter

from nautobot_remote_jobs import views

app_name = "nautobot_remote_jobs"
router = NautobotUIViewSetRouter()
router.register("job-definitions", views.JobDefinitionUIViewSet)
router.register("execution-zones", views.ExecutionZoneUIViewSet)
router.register("zone-membership-rules", views.ZoneMembershipRuleUIViewSet)
router.register("workers", views.WorkerUIViewSet)
router.register("enrollment-tokens", views.WorkerEnrollmentTokenUIViewSet)
router.register("runs", views.RemoteJobRunUIViewSet)
router.register("schedules", views.RemoteJobScheduleUIViewSet)

urlpatterns = [
    path(
        "job-definitions/<uuid:pk>/run/",
        views.JobDefinitionRunView.as_view(),
        name="jobdefinition_run",
    ),
    path(
        "runs/<uuid:pk>/cancel/",
        views.RemoteJobRunCancelView.as_view(),
        name="remotejobrun_cancel",
    ),
    path("zone-coverage/", views.ZoneCoverageReportView.as_view(), name="zone_coverage"),
    path("docs/", RedirectView.as_view(url=static("nautobot_remote_jobs/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
