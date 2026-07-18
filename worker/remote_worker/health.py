"""Tiny dependency-free ``/healthz`` HTTP endpoint (SPEC 16).

Implemented directly on :func:`asyncio.start_server`; returns 200 with a
JSON snapshot of agent state (connected, in-flight count, draining).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_MAX_HEADER_BYTES = 8192


class HealthServer:
    """Minimal HTTP/1.1 listener answering ``GET /healthz``."""

    def __init__(
        self,
        host: str,
        port: int,
        status_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._host = host
        self._port = port
        self._status_provider = status_provider
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        """Bind and start serving."""
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        logger.info("health endpoint listening on %s:%d/healthz", self._host, self._port)

    async def close(self) -> None:
        """Stop serving."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=5.0
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, asyncio.TimeoutError):
                return
            if len(head) > _MAX_HEADER_BYTES:
                return
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = request_line.split(" ")
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else ""
            if method != "GET":
                self._respond(writer, 405, {"error": "method not allowed"})
            elif path.split("?", 1)[0] in ("/healthz", "/healthz/"):
                self._respond(writer, 200, self._status_provider())
            else:
                self._respond(writer, 404, {"error": "not found"})
            await writer.drain()
        except (ConnectionError, OSError):  # pragma: no cover - client went away
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    def _respond(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "Error")
        body = json.dumps(payload).encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("latin-1") + body
        )
