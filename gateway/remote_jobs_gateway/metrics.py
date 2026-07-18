"""Prometheus metrics for the gateway (SPEC 16).

Exposed on ``/metrics`` via ``prometheus_client``:

- connection gauge, RPC frame counter by method/direction, per-method latency
  histogram (publish-to-response round trip through the app), and a rejection
  counter for rate-limit / frame-size / validation violations.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: Currently connected, authenticated worker WebSockets.
CONNECTIONS = Gauge(
    "remote_jobs_gateway_connections",
    "Number of authenticated worker WebSocket connections.",
)

#: JSON-RPC frames bridged, labeled by method and direction.
#: direction: worker_to_server | server_to_worker.
RPC_FRAMES = Counter(
    "remote_jobs_gateway_rpc_frames_total",
    "JSON-RPC frames bridged through the gateway.",
    ["method", "direction"],
)

#: Round-trip latency of worker->server requests (publish to response receipt).
RPC_LATENCY = Histogram(
    "remote_jobs_gateway_rpc_latency_seconds",
    "Latency of worker->server RPC requests through the Redis bridge.",
    ["method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

#: Frames rejected before bridging. reason: rate_limited | frame_too_large |
#: parse_error | invalid_frame.
FRAME_REJECTIONS = Counter(
    "remote_jobs_gateway_frame_rejections_total",
    "Worker frames rejected by the gateway before bridging.",
    ["reason"],
)

#: Handshake outcomes. result: accepted | rejected | error.
HANDSHAKES = Counter(
    "remote_jobs_gateway_handshakes_total",
    "Worker handshake attempts by outcome.",
    ["result"],
)


def render_metrics() -> tuple[bytes, str]:
    """Return the exposition payload and its content type for ``/metrics``."""
    return generate_latest(), CONTENT_TYPE_LATEST
