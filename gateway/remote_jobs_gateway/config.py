"""Gateway configuration via environment variables (pydantic-settings).

All settings use the ``GATEWAY_`` environment prefix, e.g. ``GATEWAY_REDIS_URL``.
"""

from __future__ import annotations

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Redis channel the gateway publishes worker->server RPC frames to (SPEC 8.4).
RPC_CHANNEL = "remote-jobs:gateway:rpc"


def worker_cmd_channel(worker_id: str) -> str:
    """Channel carrying targeted server->worker RPC frames."""
    return f"remote-jobs:worker:{worker_id}:cmd"


def worker_rsp_pattern(worker_id: str) -> str:
    """Pattern matching per-request response channels for a worker."""
    return f"remote-jobs:worker:{worker_id}:rsp:*"


def worker_rsp_channel(worker_id: str, request_id: str) -> str:
    """Channel the app publishes the result for a single worker request to."""
    return f"remote-jobs:worker:{worker_id}:rsp:{request_id}"


def zone_notify_channel(zone_id: str) -> str:
    """Fan-out channel for ``job.available`` notifications in a zone."""
    return f"remote-jobs:zone:{zone_id}:notify"


def nonce_key(worker_id: str, nonce: str) -> str:
    """Redis key used for handshake nonce replay protection (SETNX + TTL)."""
    return f"remote-jobs:gateway:nonce:{worker_id}:{nonce}"


class GatewaySettings(BaseSettings):
    """Runtime configuration for the gateway process.

    Environment variables (prefix ``GATEWAY_``):

    - ``GATEWAY_REDIS_URL``: Redis connection URL for the pub/sub bridge.
    - ``GATEWAY_APP_URL``: Base URL of the Nautobot deployment hosting the app.
    - ``GATEWAY_INTERNAL_TOKEN``: shared token authenticating the gateway to the
      app's internal verify-session endpoint. The only secret the gateway holds.
    - ``GATEWAY_BIND``: ``host:port`` to bind the ASGI server to.
    """

    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")

    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis URL for pub/sub and nonce storage.")
    app_url: str = Field(default="http://localhost:8080", description="Base URL of the Nautobot instance.")
    internal_token: str = Field(default="", description="Shared gateway auth token for the internal app API.")
    bind: str = Field(default="0.0.0.0:8001", description="host:port the gateway listens on.")

    verify_session_path: str = Field(
        default="/api/plugins/remote-jobs/internal/verify-session/",
        description="App endpoint validating worker handshakes.",
    )

    # Handshake / auth.
    handshake_timeout_seconds: float = Field(default=10.0, gt=0, description="Max wait for the handshake frame.")
    timestamp_skew_seconds: int = Field(default=300, ge=0, description="Max allowed |now - handshake timestamp|.")
    nonce_ttl_seconds: int = Field(default=600, gt=0, description="TTL of nonce replay-protection keys.")
    verify_timeout_seconds: float = Field(default=5.0, gt=0, description="HTTP timeout for verify-session calls.")

    # Bridge limits (SPEC 8.4).
    rate_limit_rps: float = Field(default=30.0, gt=0, description="Per-worker inbound frame rate limit (req/s).")
    rate_limit_burst: int = Field(default=30, gt=0, description="Token-bucket burst size for the rate limit.")
    max_frame_bytes: int = Field(default=256 * 1024, gt=0, description="Max WebSocket frame size in bytes (256 KiB).")
    rpc_response_timeout_seconds: float = Field(
        default=30.0, gt=0, description="Max wait for the app to answer a worker RPC request."
    )

    # WebSocket keepalive (protocol-level ping/pong, served by uvicorn).
    ws_ping_interval_seconds: float = Field(default=20.0, gt=0, description="Interval between WS protocol pings.")
    ws_ping_timeout_seconds: float = Field(default=20.0, gt=0, description="Grace before an unanswered ping drops the socket.")

    log_level: str = Field(default="INFO", description="Python logging level name.")

    @field_validator("bind")
    @classmethod
    def _validate_bind(cls, value: str) -> str:
        """Require a host:port bind string with a numeric port."""
        host, sep, port = value.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError("GATEWAY_BIND must look like 'host:port', e.g. '0.0.0.0:8001'")
        return value

    @property
    def bind_host(self) -> str:
        """Host part of ``bind``."""
        return self.bind.rpartition(":")[0]

    @property
    def bind_port(self) -> int:
        """Port part of ``bind``."""
        return int(self.bind.rpartition(":")[2])

    @property
    def verify_session_url(self) -> str:
        """Fully-qualified verify-session endpoint URL."""
        return self.app_url.rstrip("/") + self.verify_session_path

    def configure_logging(self) -> None:
        """Apply the configured log level to the root logger."""
        logging.basicConfig(
            level=self.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
