"""HTTP log sink (SPEC 10.1) — the default.

Posts batches to the app's worker-facing REST endpoints::

    POST {nautobot_url}/api/plugins/remote-jobs/runs/{run_id}/logs/
    POST {nautobot_url}/api/plugins/remote-jobs/runs/{run_id}/console/

Each request body is ``{"client_sequence": N, "entries": [...]}``; the
server dedupes on ``(run_id, client_sequence)`` so delivery is
at-least-once with client-side retry.

Requests are authenticated with the worker session identity using the same
HMAC scheme as the WebSocket handshake (the session secret never transits):
``X-Remote-Worker-Id``, ``X-Remote-Worker-Timestamp``,
``X-Remote-Worker-Nonce``, ``X-Remote-Worker-Signature``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

import httpx

from ..connection import compute_signature
from .base import KIND_CONSOLE, BatchingLogSink

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 0.5
_RETRY_CAP_SECONDS = 10.0


class HttpLogSink(BatchingLogSink):
    """Batched, retried delivery to the app's log ingestion endpoints."""

    def __init__(
        self,
        base_url: str,
        worker_id: str,
        secret_provider: Callable[[], str],
        tls_verify: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self._base_url = base_url.rstrip("/")
        self._worker_id = worker_id
        self._secret_provider = secret_provider
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(verify=tls_verify, timeout=15.0)

    def _headers(self) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature = compute_signature(
            self._worker_id, timestamp, nonce, self._secret_provider()
        )
        return {
            "X-Remote-Worker-Id": self._worker_id,
            "X-Remote-Worker-Timestamp": timestamp,
            "X-Remote-Worker-Nonce": nonce,
            "X-Remote-Worker-Signature": signature,
        }

    def _url(self, run_id: str, kind: str) -> str:
        suffix = "console" if kind == KIND_CONSOLE else "logs"
        return f"{self._base_url}/api/plugins/remote-jobs/runs/{run_id}/{suffix}/"

    async def _send(
        self, run_id: str, kind: str, sequence: int, entries: list[dict[str, Any]]
    ) -> None:
        url = self._url(run_id, kind)
        body = {"client_sequence": sequence, "entries": entries}
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(url, json=body, headers=self._headers())
                if response.status_code < 300:
                    return
                # 4xx (other than 429) will not improve with retries.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    logger.error(
                        "log batch rejected: HTTP %d for %s seq=%d: %s",
                        response.status_code, url, sequence, response.text[:300],
                    )
                    return
                last_error = RuntimeError(f"HTTP {response.status_code}")
            except httpx.HTTPError as exc:
                last_error = exc
            delay = min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (2 ** attempt))
            logger.warning(
                "log batch delivery failed (%s), retrying in %.1fs (%d/%d)",
                last_error, delay, attempt + 1, self._max_attempts,
            )
            await asyncio.sleep(delay)
        raise RuntimeError(
            f"giving up delivering {kind} batch seq={sequence} for run {run_id}: {last_error}"
        )

    async def _close(self) -> None:
        await self._client.aclose()
