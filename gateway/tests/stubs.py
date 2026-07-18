"""In-memory test doubles: a minimal async Redis stub and a WebSocket recorder."""

from __future__ import annotations

import time
from typing import Any


class InMemoryRedis:
    """Async stub covering the Redis surface the gateway uses.

    Supports ``set(nx=..., ex=...)`` with real TTL expiry, ``publish`` (records
    every message), and ``ping``. Not thread-safe; single-event-loop tests only.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self.published: list[tuple[str, str]] = []
        self.ping_ok = True

    def _expired(self, key: str) -> bool:
        value = self._store.get(key)
        if value is None:
            return True
        _, expires = value
        if expires is not None and time.monotonic() >= expires:
            del self._store[key]
            return True
        return False

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        """Mimic redis SET; returns True on success, None on failed NX."""
        if nx and not self._expired(key):
            return None
        expires = time.monotonic() + ex if ex is not None else None
        self._store[key] = (value, expires)
        return True

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._store[key][0]

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def ping(self) -> bool:
        if not self.ping_ok:
            raise ConnectionError("redis down")
        return True

    async def aclose(self) -> None:
        return None


class RecordingWebSocket:
    """Records everything the bridge sends to the worker."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def make_settings(**overrides: Any):
    """Build GatewaySettings isolated from ambient GATEWAY_* env vars."""
    from remote_jobs_gateway.config import GatewaySettings

    defaults: dict[str, Any] = {
        "redis_url": "redis://test:6379/0",
        "app_url": "http://app.test",
        "internal_token": "test-internal-token",
        "_env_file": None,
    }
    defaults.update(overrides)
    return GatewaySettings(**defaults)
