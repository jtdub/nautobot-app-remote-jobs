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
    description = "Remote job execution for Nautobot: isolated containers, zone-based dispatch, API-scoped tokens."
    base_url = "remote-jobs"
    required_settings = []
    default_settings = {
        # Heartbeat TTL for Worker.status (SPEC 4.4).
        "worker_ttl_seconds": 90,
        # Claim lease duration (SPEC 6.2).
        "lease_seconds": 120,
        # Minimum interval between Worker.last_seen writes (SPEC 4.4).
        "last_seen_throttle_seconds": 15,
        # Added to timeout+grace for scoped token expiry (SPEC 7.3).
        "token_extra_ttl_seconds": 60,
        # Shared secret the gateway presents on /internal/verify-session/.
        "gateway_internal_token": "",
        # Public URL advertised to workers in job offers (SPEC 8.1).
        "nautobot_url": "",
        # Redis URL for gateway bridging; defaults to CELERY_BROKER_URL.
        "redis_url": None,
        # Delivered to agents in worker.hello (SPEC 10).
        "log_sink_config": {"type": "http"},
    }
    docs_view_name = "plugins:nautobot_remote_jobs:docs"
    searchable_models = ["jobdefinition", "executionzone", "worker", "remotejobrun"]

    def ready(self):
        """Register the cancel strategy and Celery beat entries (SPEC 11, 4.7, 6.2)."""
        super().ready()

        # Guarded CancelFactory registration; falls back to the app Cancel button.
        from nautobot_remote_jobs.cancel import register_cancel_strategy

        register_cancel_strategy()

        # Beat entries for the scheduler and the lease reaper.
        try:
            from nautobot.core.celery import app as celery_app

            celery_app.conf.beat_schedule.setdefault(
                "remote_jobs_dispatch_scheduled",
                {"task": "remote_jobs.dispatch_scheduled", "schedule": 60.0},
            )
            celery_app.conf.beat_schedule.setdefault(
                "remote_jobs_reap_expired",
                {"task": "remote_jobs.reap_expired", "schedule": 30.0},
            )
        except Exception:  # noqa: BLE001 - beat registration must not break startup
            import logging

            logging.getLogger(__name__).warning("Could not register remote-jobs beat schedule entries", exc_info=True)


config = RemoteJobsConfig  # pylint:disable=invalid-name
