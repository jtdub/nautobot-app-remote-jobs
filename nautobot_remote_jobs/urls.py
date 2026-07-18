"""Django urlpatterns declaration for nautobot_remote_jobs app."""

from django.templatetags.static import static
from django.urls import path
from django.views.generic import RedirectView
from nautobot.apps.urls import NautobotUIViewSetRouter


# Uncomment the following line if you have views to import
# from nautobot_remote_jobs import views


app_name = "nautobot_remote_jobs"
router = NautobotUIViewSetRouter()

# Here is an example of how to register a viewset, you will want to replace views.RemoteJobsUIViewSet with your viewset
# router.register("nautobot_remote_jobs", views.RemoteJobsUIViewSet)


urlpatterns = [
    path("docs/", RedirectView.as_view(url=static("nautobot_remote_jobs/docs/index.html")), name="docs"),
]

urlpatterns += router.urls
