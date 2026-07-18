"""App declaration for nautobot_remote_jobs."""

# Metadata is inherited from Nautobot. If not including Nautobot in the environment, this should be added
from importlib import metadata

from nautobot.apps import NautobotAppConfig

__version__ = metadata.version(__name__)


class RemoteJobsConfig(NautobotAppConfig):
    """App configuration for the nautobot_remote_jobs app."""

    name = "nautobot_remote_jobs"
    verbose_name = "Remote Jobs"
    version = __version__
    author = "James Williams"
    description = "Remote job execution for Nautobot: isolated containers, zone-based dispatch, API-scoped tokens.."
    base_url = "remote-jobs"
    required_settings = []
    default_settings = {}
    docs_view_name = "plugins:nautobot_remote_jobs:docs"
    searchable_models = []


config = RemoteJobsConfig  # pylint:disable=invalid-name
