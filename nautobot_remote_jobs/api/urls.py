"""API URL routing: /api/plugins/remote-jobs/ (SPEC 5)."""

from django.urls import path
from nautobot.apps.api import OrderedDefaultRouter

from nautobot_remote_jobs.api import views, worker_views

router = OrderedDefaultRouter()
router.register("job-definitions", views.JobDefinitionViewSet)
router.register("execution-zones", views.ExecutionZoneViewSet)
router.register("zone-membership-rules", views.ZoneMembershipRuleViewSet)
router.register("workers", views.WorkerViewSet)
router.register("enrollment-tokens", views.WorkerEnrollmentTokenViewSet)
router.register("runs", views.RemoteJobRunViewSet)
router.register("artifacts", views.RunArtifactViewSet)
router.register("schedules", views.RemoteJobScheduleViewSet)

app_name = "nautobot_remote_jobs-api"

urlpatterns = [
    # Worker-facing endpoints (SPEC 5). Registered before the router so
    # "workers/self/" wins over the workers/{pk}/ route.
    path("enroll/", worker_views.EnrollView.as_view(), name="worker_enroll"),
    path("workers/self/", worker_views.WorkerSelfView.as_view(), name="worker_self"),
    path("runs/<uuid:pk>/logs/", worker_views.RunLogsView.as_view(), name="run_logs"),
    path("runs/<uuid:pk>/console/", worker_views.RunConsoleView.as_view(), name="run_console"),
    path("runs/<uuid:pk>/artifacts/", worker_views.RunArtifactsView.as_view(), name="run_artifacts"),
    path(
        "runs/<uuid:pk>/artifacts/<uuid:artifact_id>/content/",
        worker_views.RunArtifactContentView.as_view(),
        name="run_artifact_content",
    ),
    path(
        "runs/<uuid:pk>/artifacts/<uuid:artifact_id>/complete/",
        worker_views.RunArtifactCompleteView.as_view(),
        name="run_artifact_complete",
    ),
    path(
        "internal/verify-session/",
        worker_views.VerifySessionView.as_view(),
        name="verify_session",
    ),
]

urlpatterns += router.urls
