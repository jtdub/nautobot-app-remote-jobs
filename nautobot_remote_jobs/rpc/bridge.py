"""Redis bridge consumer: gateway RPC channel -> handlers -> response channels (SPEC 8.4).

Run via `nautobot-server remote_jobs_rpc_consumer`. A dedicated lightweight
consumer process was chosen over Celery-backed handlers to hit the < 250ms
claim round-trip target (SPEC open question 1): a subscribe loop avoids broker
queue latency entirely.
"""

import json
import logging
import signal
import threading

from nautobot_remote_jobs.constants import CHANNEL_GATEWAY_RPC, CHANNEL_WORKER_RSP
from nautobot_remote_jobs.dispatch.notify import get_redis_client
from nautobot_remote_jobs.rpc.handlers import dispatch_rpc

logger = logging.getLogger(__name__)


class RPCBridgeConsumer:
    """Consumes worker->server frames published by the gateway and answers them."""

    def __init__(self, redis_client=None, workers=4):
        self.redis = redis_client or get_redis_client()
        self.workers = workers
        self._stop = threading.Event()

    def stop(self, *args):  # pylint: disable=unused-argument
        """Signal the run loop to exit."""
        self._stop.set()

    def run(self):
        """Blocking subscribe loop; handles frames on a small thread pool."""
        from concurrent.futures import ThreadPoolExecutor

        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(CHANNEL_GATEWAY_RPC)
        logger.info("RPC bridge consumer subscribed to %s", CHANNEL_GATEWAY_RPC)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while not self._stop.is_set():
                message = pubsub.get_message(timeout=1.0)
                if message is None or message.get("type") != "message":
                    continue
                pool.submit(self._handle_message, message["data"])
        pubsub.close()
        logger.info("RPC bridge consumer stopped")

    def _handle_message(self, raw):
        try:
            envelope = json.loads(raw)
            worker_id = envelope["worker_id"]
            frame = envelope["frame"]
        except (ValueError, KeyError, TypeError):
            logger.warning("Malformed envelope on %s: %.200s", CHANNEL_GATEWAY_RPC, raw)
            return
        # Ensure this thread has a usable DB connection lifecycle.
        from django.db import close_old_connections

        close_old_connections()
        try:
            response = dispatch_rpc(worker_id, frame)
        finally:
            close_old_connections()
        if response is None:
            return
        channel = CHANNEL_WORKER_RSP.format(worker_id=worker_id, request_id=frame.get("id"))
        try:
            self.redis.publish(channel, json.dumps(response))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to publish RPC response to %s", channel, exc_info=True)
