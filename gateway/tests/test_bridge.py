"""Bridge tests: rate limiting, frame-size rejection, and frame routing."""

from __future__ import annotations

import asyncio
import json

import pytest

from remote_jobs_gateway import rpc
from remote_jobs_gateway.bridge import RateLimiter, WorkerBridge
from remote_jobs_gateway.config import RPC_CHANNEL

from .stubs import InMemoryRedis, RecordingWebSocket, make_settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRateLimiter:
    def test_burst_then_reject(self):
        clock = FakeClock()
        limiter = RateLimiter(rate=30.0, burst=5, clock=clock)
        assert all(limiter.allow() for _ in range(5))
        assert limiter.allow() is False

    def test_refills_over_time(self):
        clock = FakeClock()
        limiter = RateLimiter(rate=30.0, burst=1, clock=clock)
        assert limiter.allow() is True
        assert limiter.allow() is False
        clock.advance(1.0 / 30.0)
        assert limiter.allow() is True

    def test_bucket_capped_at_burst(self):
        clock = FakeClock()
        limiter = RateLimiter(rate=30.0, burst=2, clock=clock)
        clock.advance(100.0)  # long idle: still only 'burst' tokens
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is False

    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            RateLimiter(rate=0, burst=1)
        with pytest.raises(ValueError):
            RateLimiter(rate=1, burst=0)


def make_bridge(**settings_overrides):
    settings = make_settings(**settings_overrides)
    ws = RecordingWebSocket()
    redis = InMemoryRedis()
    bridge = WorkerBridge(websocket=ws, redis=redis, settings=settings, worker_id="w-1", zone_id="z-1")
    return bridge, ws, redis


def request_frame(request_id="req-1", method="job.claim", params=None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})


class TestFrameSizeLimit:
    async def test_oversized_frame_rejected(self):
        bridge, ws, redis = make_bridge(max_frame_bytes=1024)
        big = json.dumps({"jsonrpc": "2.0", "method": "job.status", "params": {"blob": "x" * 2048}})
        await bridge.handle_worker_frame(big)
        assert redis.published == []  # nothing bridged
        assert len(ws.sent) == 1
        err = json.loads(ws.sent[0])
        assert err["error"]["code"] == rpc.ERR_FRAME_TOO_LARGE

    async def test_frame_at_limit_accepted(self):
        bridge, ws, redis = make_bridge(max_frame_bytes=4096)
        frame = json.dumps({"jsonrpc": "2.0", "method": "job.status", "params": {"pad": "x" * 100}})
        await bridge.handle_worker_frame(frame)
        assert len(redis.published) == 1
        assert ws.sent == []


class TestRateLimitRejection:
    async def test_over_rate_frames_get_error(self):
        bridge, ws, redis = make_bridge(rate_limit_rps=30.0, rate_limit_burst=3)
        for i in range(5):
            await bridge.handle_worker_frame(
                json.dumps({"jsonrpc": "2.0", "method": "job.status", "params": {"n": i}})
            )
        assert len(redis.published) == 3  # burst allowed through
        errors = [json.loads(s) for s in ws.sent]
        assert len(errors) == 2
        assert all(e["error"]["code"] == rpc.ERR_RATE_LIMITED for e in errors)

    async def test_rate_limited_request_echoes_id(self):
        bridge, ws, redis = make_bridge(rate_limit_burst=1)
        await bridge.handle_worker_frame(request_frame(request_id="keep"))
        await bridge.handle_worker_frame(request_frame(request_id="limited"))
        err = json.loads(ws.sent[-1])
        assert err["id"] == "limited"
        assert err["error"]["code"] == rpc.ERR_RATE_LIMITED
        # cleanup pending waiter task
        for task in list(bridge._response_tasks):
            task.cancel()
        await asyncio.gather(*bridge._response_tasks, return_exceptions=True)


class TestFrameRouting:
    async def test_invalid_json_rejected(self):
        bridge, ws, redis = make_bridge()
        await bridge.handle_worker_frame("{broken")
        assert redis.published == []
        assert json.loads(ws.sent[0])["error"]["code"] == rpc.PARSE_ERROR

    async def test_invalid_frame_rejected_with_id(self):
        bridge, ws, redis = make_bridge()
        await bridge.handle_worker_frame(json.dumps({"jsonrpc": "1.0", "id": "x1", "method": "m"}))
        err = json.loads(ws.sent[0])
        assert err["error"]["code"] == rpc.INVALID_REQUEST
        assert err["id"] == "x1"
        assert redis.published == []

    async def test_notification_published_fire_and_forget(self):
        bridge, ws, redis = make_bridge()
        await bridge.handle_worker_frame(json.dumps({"jsonrpc": "2.0", "method": "job.available.ack"}))
        channel, message = redis.published[0]
        assert channel == RPC_CHANNEL
        envelope = json.loads(message)
        assert envelope["worker_id"] == "w-1"
        assert envelope["zone_id"] == "z-1"
        assert envelope["frame"]["method"] == "job.available.ack"
        assert ws.sent == []
        assert bridge._pending == {}

    async def test_worker_response_frame_published(self):
        """Replies to server->worker RPCs (e.g. worker.ping) bridge fire-and-forget."""
        bridge, ws, redis = make_bridge()
        await bridge.handle_worker_frame(json.dumps({"jsonrpc": "2.0", "id": "ping-1", "result": {}}))
        channel, message = redis.published[0]
        assert channel == RPC_CHANNEL
        assert json.loads(message)["frame"]["result"] == {}
        assert bridge._pending == {}

    async def test_request_publishes_and_awaits_response(self):
        bridge, ws, redis = make_bridge(rpc_response_timeout_seconds=5.0)
        await bridge.handle_worker_frame(request_frame(request_id="req-42"))
        assert "req-42" in bridge._pending
        channel, message = redis.published[0]
        assert channel == RPC_CHANNEL
        assert json.loads(message)["frame"]["id"] == "req-42"

        # Simulate the app's response arriving on the rsp channel.
        response = json.dumps({"jsonrpc": "2.0", "id": "req-42", "result": {"offers": []}})
        bridge._pending["req-42"].set_result(response)
        bridge._pending.pop("req-42")
        await asyncio.gather(*bridge._response_tasks)
        assert ws.sent == [response]

    async def test_request_timeout_produces_error(self):
        bridge, ws, redis = make_bridge(rpc_response_timeout_seconds=0.05)
        await bridge.handle_worker_frame(request_frame(request_id="req-slow"))
        await asyncio.gather(*bridge._response_tasks)
        err = json.loads(ws.sent[0])
        assert err["error"]["code"] == rpc.ERR_UPSTREAM_TIMEOUT
        assert err["id"] == "req-slow"
        assert bridge._pending == {}
