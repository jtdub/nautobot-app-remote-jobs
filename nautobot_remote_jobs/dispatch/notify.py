"""Redis pub/sub notifications toward the gateway (SPEC 6.2, 8.4)."""

import json
import logging

from django.conf import settings

from nautobot_remote_jobs.constants import CHANNEL_WORKER_CMD, CHANNEL_ZONE_NOTIFY

logger = logging.getLogger(__name__)


def get_redis_client():
    """Redis client for gateway bridging.

    Uses the app-configured URL, falling back to the Nautobot Celery broker URL
    which is Redis in standard deployments.
    """
    import redis

    # redis_url defaults to None in the app's default_settings, so a plain
    # .get(..., fallback) would return None (the key exists) rather than the
    # fallback; coalesce explicitly to the Celery broker URL.
    config = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {})
    url = config.get("redis_url") or getattr(settings, "CELERY_BROKER_URL", None) or "redis://localhost:6379/0"
    return redis.Redis.from_url(url)


def publish_work_available(zone):
    """Publish a work-available nudge on the zone channel; gateway relays job.available (SPEC 6.2)."""
    frame = {"jsonrpc": "2.0", "method": "job.available", "params": {"zone": str(zone.name)}}
    _publish(CHANNEL_ZONE_NOTIFY.format(zone_id=str(zone.pk)), frame)


def publish_worker_command(worker, method, params):
    """Publish a targeted server->worker JSON-RPC frame on the worker command channel."""
    frame = {"jsonrpc": "2.0", "method": method, "params": params}
    _publish(CHANNEL_WORKER_CMD.format(worker_id=str(worker.pk)), frame)


def publish_cancel(worker, run, mode="graceful"):
    """Send job.cancel to the worker executing the run (SPEC 8.2, 11)."""
    publish_worker_command(worker, "job.cancel", {"run_id": str(run.pk), "mode": mode})


def publish_drain(worker):
    """Send worker.drain (SPEC 8.2)."""
    publish_worker_command(worker, "worker.drain", {})


def _publish(channel, frame):
    try:
        client = get_redis_client()
        client.publish(channel, json.dumps(frame))
    except Exception:  # noqa: BLE001 - notification loss must never break dispatch state
        logger.warning("Failed to publish to Redis channel %s", channel, exc_info=True)
