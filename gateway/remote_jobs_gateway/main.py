"""ASGI application factory and uvicorn entrypoint for remote-jobs-gateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect

from . import __version__, metrics
from .auth import (
    AuthenticationFailed,
    HandshakeError,
    SessionVerifier,
    check_nonce,
    check_timestamp,
    parse_handshake,
)
from .bridge import WorkerBridge
from .config import GatewaySettings

logger = logging.getLogger(__name__)

#: WebSocket close codes (application range).
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_HANDSHAKE = 4400
CLOSE_SERVER_ERROR = 1011


def create_app(settings: GatewaySettings | None = None) -> FastAPI:
    """Build the gateway ASGI application.

    Args:
        settings: optional pre-built settings (tests); defaults to env-derived.
    """
    settings = settings or GatewaySettings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        app.state.http = httpx.AsyncClient()
        app.state.verifier = SessionVerifier(app.state.http, settings)
        logger.info("gateway %s starting (redis=%s app=%s)", __version__, settings.redis_url, settings.app_url)
        try:
            yield
        finally:
            await app.state.http.aclose()
            await app.state.redis.aclose()
            logger.info("gateway stopped")

    app = FastAPI(title="remote-jobs-gateway", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness/readiness: verifies Redis connectivity."""
        try:
            await asyncio.wait_for(app.state.redis.ping(), timeout=2.0)
        except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
            logger.warning("healthz redis ping failed: %r", exc)
            return Response(
                content=json.dumps({"status": "unhealthy", "redis": "unreachable"}),
                status_code=503,
                media_type="application/json",
            )
        return Response(
            content=json.dumps({"status": "ok", "redis": "ok", "version": __version__}),
            media_type="application/json",
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Prometheus exposition endpoint."""
        payload, content_type = metrics.render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.websocket("/ws/worker")
    async def ws_worker(websocket: WebSocket) -> None:
        """Worker WebSocket: authenticate, then bridge JSON-RPC frames."""
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=settings.handshake_timeout_seconds
            )
        except asyncio.TimeoutError:
            metrics.HANDSHAKES.labels(result="rejected").inc()
            await websocket.close(code=CLOSE_BAD_HANDSHAKE, reason="handshake timeout")
            return
        except WebSocketDisconnect:
            return

        try:
            payload = json.loads(raw)
            handshake = parse_handshake(payload)
            check_timestamp(handshake, settings.timestamp_skew_seconds)
            await check_nonce(app.state.redis, handshake, settings.nonce_ttl_seconds)
        except (ValueError, HandshakeError) as exc:
            metrics.HANDSHAKES.labels(result="rejected").inc()
            logger.info("handshake rejected: %s", exc)
            await websocket.close(code=CLOSE_BAD_HANDSHAKE, reason="invalid handshake")
            return

        try:
            session = await app.state.verifier.verify(handshake)
        except AuthenticationFailed as exc:
            metrics.HANDSHAKES.labels(result="rejected").inc()
            logger.info("authentication failed for worker %s: %s", handshake.worker_id, exc)
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="authentication failed")
            return

        metrics.HANDSHAKES.labels(result="accepted").inc()
        await websocket.send_text(
            json.dumps({"authenticated": True, "worker_id": session.worker_id, "zone_id": session.zone_id})
        )
        logger.info("worker %s authenticated (zone %s)", session.worker_id, session.zone_id)

        bridge = WorkerBridge(
            websocket=websocket,
            redis=app.state.redis,
            settings=settings,
            worker_id=session.worker_id,
            zone_id=session.zone_id,
        )
        metrics.CONNECTIONS.inc()
        try:
            await bridge.run()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - never let one worker kill the server
            logger.exception("bridge crashed for worker %s", session.worker_id)
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_SERVER_ERROR)
        finally:
            metrics.CONNECTIONS.dec()
            logger.info("worker %s disconnected", session.worker_id)

    return app


def run() -> None:
    """Console entrypoint: run the gateway under uvicorn.

    WebSocket protocol-level ping/pong keepalive is handled by uvicorn using
    the configured intervals; application-level ``worker.ping`` frames are
    bridged like any other server->worker RPC.
    """
    import uvicorn

    settings = GatewaySettings()
    settings.configure_logging()
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        ws_ping_interval=settings.ws_ping_interval_seconds,
        ws_ping_timeout=settings.ws_ping_timeout_seconds,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
