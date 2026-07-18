"""Gateway WebSocket connection: HMAC handshake, JSON-RPC, backoff (SPEC 7.2, 8).

The connection is treated as expendable: on any error the agent reconnects
with jittered exponential backoff (1s..60s) and the ``on_connected``
callback re-sends ``worker.hello`` including in-flight run ids.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import ssl
import time
import uuid
from typing import Any, Awaitable, Callable

try:  # imported lazily so unit tests run without the dependency installed
    import websockets
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
except ImportError:  # pragma: no cover - exercised only in minimal envs
    websockets = None  # type: ignore[assignment]

    class _WsConnectionClosed(Exception):
        """Placeholder when websockets is unavailable."""


from . import rpc

logger = logging.getLogger(__name__)

WS_WORKER_PATH = "/ws/worker"
MAX_FRAME_BYTES = 256 * 1024  # gateway max frame size (SPEC 8.4)
DEFAULT_CALL_TIMEOUT = 30.0
#: A connection older than this resets the reconnect backoff.
STABLE_CONNECTION_SECONDS = 30.0


class ConnectionClosedError(ConnectionError):
    """Raised when a call cannot complete because the WS is down."""


def compute_signature(worker_id: str, timestamp: int | str, nonce: str, session_secret: str) -> str:
    """HMAC-SHA256 hex digest over ``"{worker_id}:{timestamp}:{nonce}"``.

    Keyed by the session secret; the secret itself never transits the wire.
    """
    message = f"{worker_id}:{timestamp}:{nonce}".encode("utf-8")
    return hmac.new(session_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_handshake(
    worker_id: str,
    session_secret: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Build the first frame sent on a fresh WebSocket connection."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = uuid.uuid4().hex if nonce is None else nonce
    return {
        "worker_id": worker_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": compute_signature(worker_id, timestamp, nonce, session_secret),
    }


def gateway_ws_url(gateway_url: str) -> str:
    """Derive the ``/ws/worker`` WebSocket URL from the gateway base URL."""
    url = gateway_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    if not url.endswith(WS_WORKER_PATH):
        url += WS_WORKER_PATH
    return url


class GatewayConnection:
    """Owns the WebSocket to the gateway and the JSON-RPC request lifecycle."""

    def __init__(
        self,
        url: str,
        worker_id: str,
        secret_provider: Callable[[], str],
        on_connected: Callable[["GatewayConnection"], Awaitable[None]],
        on_server_call: Callable[[str, dict[str, Any]], Awaitable[Any]],
        backoff_min: float = 1.0,
        backoff_max: float = 60.0,
        tls_verify: bool = True,
    ) -> None:
        self._url = url
        self._worker_id = worker_id
        self._secret_provider = secret_provider
        self._on_connected = on_connected
        self._on_server_call = on_server_call
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._tls_verify = tls_verify
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[rpc.RpcResponse]] = {}
        self._closing = False
        self.connected = False

    # ------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        """Connect-loop with jittered exponential backoff (1s..60s)."""
        if websockets is None:  # pragma: no cover
            raise RuntimeError("the 'websockets' package is required to connect")
        attempt = 0
        while not self._closing:
            connected_at = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("gateway connection error: %s", exc)
            if self._closing:
                break
            if time.monotonic() - connected_at >= STABLE_CONNECTION_SECONDS:
                attempt = 0
            delay = self._backoff_delay(attempt)
            attempt += 1
            logger.info("reconnecting to gateway in %.1fs (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        ceiling = min(self._backoff_max, self._backoff_min * (2**attempt))
        return random.uniform(max(self._backoff_min / 2, ceiling / 2), ceiling)  # noqa: S311 - jitter, not crypto

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._url.startswith("wss://"):
            return None
        context = ssl.create_default_context()
        if not self._tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _connect_once(self) -> None:
        logger.info("connecting to gateway %s", self._url)
        async with websockets.connect(
            self._url,
            ssl=self._ssl_context(),
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            try:
                handshake = build_handshake(self._worker_id, self._secret_provider())
                await ws.send(json.dumps(handshake))
                self.connected = True
                read_task = asyncio.create_task(self._read_loop(ws), name="ws-read-loop")
                try:
                    await self._on_connected(self)
                    await read_task
                finally:
                    read_task.cancel()
                    try:
                        await read_task
                    except (asyncio.CancelledError, Exception):  # noqa: S110 - reader teardown
                        pass
            finally:
                self.connected = False
                self._ws = None
                self._fail_pending(ConnectionClosedError("gateway connection lost"))

    async def close(self) -> None:
        """Stop reconnecting and close the current socket."""
        self._closing = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: S110  # pragma: no cover - best effort
                pass

    # -------------------------------------------------------------- framing

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                frame = rpc.decode(raw)
            except rpc.RpcError as exc:
                logger.warning("dropping undecodable frame: %s", exc)
                continue
            if isinstance(frame, rpc.RpcResponse):
                self._resolve(frame)
            else:
                asyncio.create_task(
                    self._handle_server_frame(frame),
                    name=f"rpc-{frame.method}",
                )

    def _resolve(self, response: rpc.RpcResponse) -> None:
        future = self._pending.pop(response.id or "", None)
        if future is None:
            logger.debug("response for unknown request id %s", response.id)
            return
        if not future.done():
            future.set_result(response)

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _handle_server_frame(self, request: rpc.RpcRequest) -> None:
        """Dispatch a server->worker request/notification to the agent."""
        try:
            result = await self._on_server_call(request.method, request.params)
        except rpc.RpcError as exc:
            if not request.is_notification:
                await self._send_frame(rpc.error_frame(request.id, exc.code, exc.message, exc.data))
            return
        except Exception:
            logger.exception("error handling server call %s", request.method)
            if not request.is_notification:
                await self._send_frame(rpc.error_frame(request.id, rpc.INTERNAL_ERROR))
            return
        if not request.is_notification:
            await self._send_frame(rpc.result_frame(request.id, result))

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or not self.connected:
            raise ConnectionClosedError("not connected to gateway")
        try:
            await ws.send(rpc.encode(frame))
        except (_WsConnectionClosed, OSError) as exc:
            raise ConnectionClosedError(str(exc)) from exc

    # ----------------------------------------------------------------- RPC

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> Any:
        """Send a request and await its result.

        Raises:
            ConnectionClosedError: if disconnected before a response arrives.
            rpc.RpcError: if the server returns a JSON-RPC error.
        """
        frame = rpc.request_frame(method, params)
        request_id = frame["id"]
        future: asyncio.Future[rpc.RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_frame(frame)
            response = await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ConnectionClosedError(f"timeout waiting for {method} response") from exc
        finally:
            self._pending.pop(request_id, None)
        return response.raise_for_error()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        await self._send_frame(rpc.notification_frame(method, params))
