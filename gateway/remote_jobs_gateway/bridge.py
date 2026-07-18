"""WebSocket <-> Redis pub/sub bridge for one authenticated worker (SPEC 8.4).

Frame routing:

- worker->server *requests* (frames with an ``id``) are published to
  ``remote-jobs:gateway:rpc`` as ``{"worker_id", "zone_id", "frame"}``; the
  gateway then awaits the app's reply on
  ``remote-jobs:worker:{worker_id}:rsp:{request_id}`` and forwards it to the WS.
- worker->server *notifications* (no ``id``) and *responses* (replies to
  server->worker RPCs such as ``worker.ping``) are published fire-and-forget
  to the same rpc channel.
- ``remote-jobs:worker:{worker_id}:cmd`` (targeted server->worker RPCs) and
  ``remote-jobs:zone:{zone_id}:notify`` (fan-out ``job.available``) are
  subscribed and every message is forwarded verbatim to the WS.

The bridge enforces the per-worker rate limit and the max frame size, replying
with JSON-RPC error responses on violation. It keeps no state beyond in-flight
request futures; dispatch state lives in the app's database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import metrics, rpc
from .config import (
    RPC_CHANNEL,
    GatewaySettings,
    worker_cmd_channel,
    worker_rsp_pattern,
    zone_notify_channel,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token-bucket rate limiter (``rate`` tokens/second, ``burst`` capacity)."""

    def __init__(self, rate: float, burst: float, clock=time.monotonic) -> None:
        if rate <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = burst
        self._updated = clock()

    def allow(self) -> bool:
        """Consume one token if available; return False when rate-limited."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class WorkerBridge:
    """Bridges one authenticated worker WebSocket to Redis pub/sub."""

    def __init__(
        self,
        websocket: Any,
        redis: Any,
        settings: GatewaySettings,
        worker_id: str,
        zone_id: str,
    ) -> None:
        self._ws = websocket
        self._redis = redis
        self._settings = settings
        self.worker_id = worker_id
        self.zone_id = zone_id
        self._rate_limiter = RateLimiter(settings.rate_limit_rps, settings.rate_limit_burst)
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._response_tasks: set[asyncio.Task[None]] = set()
        self._rsp_prefix = f"remote-jobs:worker:{worker_id}:rsp:"

    async def run(self) -> None:
        """Bridge until the WebSocket closes or a fatal error occurs."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(worker_cmd_channel(self.worker_id), zone_notify_channel(self.zone_id))
            await pubsub.psubscribe(worker_rsp_pattern(self.worker_id))
            ws_task = asyncio.create_task(self._ws_reader(), name=f"ws-reader:{self.worker_id}")
            redis_task = asyncio.create_task(self._redis_reader(pubsub), name=f"redis-reader:{self.worker_id}")
            done, pending = await asyncio.wait({ws_task, redis_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    logger.warning("bridge task for worker %s failed: %r", self.worker_id, exc)
        finally:
            for task in self._response_tasks:
                task.cancel()
            if self._response_tasks:
                await asyncio.gather(*self._response_tasks, return_exceptions=True)
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("pubsub close failed for worker %s", self.worker_id, exc_info=True)
            logger.info("bridge closed for worker %s", self.worker_id)

    # ------------------------------------------------------------------ #
    # worker -> server                                                    #
    # ------------------------------------------------------------------ #

    async def _ws_reader(self) -> None:
        """Read frames from the WebSocket until disconnect."""
        while True:
            raw = await self._ws.receive_text()
            await self.handle_worker_frame(raw)

    async def handle_worker_frame(self, raw: str) -> None:
        """Validate, rate-limit, and route one inbound worker frame."""
        if len(raw.encode("utf-8")) > self._settings.max_frame_bytes:
            metrics.FRAME_REJECTIONS.labels(reason="frame_too_large").inc()
            await self._send(
                rpc.error_response(
                    None,
                    rpc.ERR_FRAME_TOO_LARGE,
                    f"frame exceeds {self._settings.max_frame_bytes} bytes",
                )
            )
            return

        if not self._rate_limiter.allow():
            metrics.FRAME_REJECTIONS.labels(reason="rate_limited").inc()
            request_id = self._extract_id(raw)
            await self._send(
                rpc.error_response(
                    request_id,
                    rpc.ERR_RATE_LIMITED,
                    f"rate limit exceeded ({self._settings.rate_limit_rps:g} req/s)",
                )
            )
            return

        try:
            frame = rpc.parse_frame(raw)
            frame_type = rpc.classify_frame(frame)
        except rpc.FrameError as exc:
            reason = "parse_error" if exc.code == rpc.PARSE_ERROR else "invalid_frame"
            metrics.FRAME_REJECTIONS.labels(reason=reason).inc()
            await self._send(rpc.error_response(exc.request_id, exc.code, str(exc)))
            return

        method = frame.get("method", "<response>")
        metrics.RPC_FRAMES.labels(method=method, direction="worker_to_server").inc()

        envelope = json.dumps({"worker_id": self.worker_id, "zone_id": self.zone_id, "frame": frame})

        if frame_type is rpc.FrameType.REQUEST:
            await self._publish_request(frame, envelope)
        else:
            # Notifications and responses to server->worker RPCs: fire-and-forget.
            await self._redis.publish(RPC_CHANNEL, envelope)

    async def _publish_request(self, frame: dict[str, Any], envelope: str) -> None:
        """Publish a worker request and spawn a waiter for the app's response."""
        request_id = str(frame["id"])
        method = str(frame.get("method"))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[request_id] = future
        started = time.monotonic()
        await self._redis.publish(RPC_CHANNEL, envelope)
        task = asyncio.create_task(
            self._await_response(frame["id"], request_id, method, future, started),
            name=f"rpc-wait:{self.worker_id}:{request_id}",
        )
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _await_response(
        self,
        original_id: Any,
        request_id: str,
        method: str,
        future: asyncio.Future[str],
        started: float,
    ) -> None:
        """Wait for the app's response on the rsp channel and relay it."""
        try:
            payload = await asyncio.wait_for(future, timeout=self._settings.rpc_response_timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.warning("RPC %s (id=%s) from worker %s timed out", method, request_id, self.worker_id)
            await self._send(
                rpc.error_response(original_id, rpc.ERR_UPSTREAM_TIMEOUT, f"no response for {method!r} in time")
            )
            return
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        metrics.RPC_LATENCY.labels(method=method).observe(time.monotonic() - started)
        await self._send_text(payload)

    # ------------------------------------------------------------------ #
    # server -> worker                                                    #
    # ------------------------------------------------------------------ #

    async def _redis_reader(self, pubsub: Any) -> None:
        """Dispatch Redis pub/sub messages: responses to waiters, RPCs to the WS."""
        async for message in pubsub.listen():
            if message is None or message.get("type") not in ("message", "pmessage"):
                continue
            channel = self._as_str(message.get("channel"))
            data = self._as_str(message.get("data"))
            if channel.startswith(self._rsp_prefix):
                request_id = channel[len(self._rsp_prefix):]
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(data)
                else:
                    logger.debug("dropping late/unmatched response for request %s", request_id)
                continue
            # cmd or zone notify channel: forward verbatim (includes worker.ping).
            method = self._extract_method(data)
            metrics.RPC_FRAMES.labels(method=method, direction="server_to_worker").inc()
            await self._send_text(data)

    # ------------------------------------------------------------------ #
    # helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _send(self, obj: dict[str, Any]) -> None:
        """Serialize and send a JSON object over the WebSocket."""
        await self._send_text(json.dumps(obj))

    async def _send_text(self, text: str) -> None:
        """Send raw text over the WebSocket, serialized across tasks."""
        async with self._send_lock:
            await self._ws.send_text(text)

    @staticmethod
    def _extract_id(raw: str) -> Any:
        """Best-effort extraction of a frame id for error responses."""
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(obj, dict) and isinstance(obj.get("id"), (str, int)):
            return obj["id"]
        return None

    @staticmethod
    def _extract_method(raw: str) -> str:
        """Best-effort extraction of a method name for metrics labels."""
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return "<invalid>"
        if isinstance(obj, dict) and isinstance(obj.get("method"), str):
            return obj["method"]
        return "<response>"

    @staticmethod
    def _as_str(value: Any) -> str:
        """Normalize Redis pub/sub payloads (bytes or str) to str."""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)
