"""Worker session handshake: HMAC challenge validation via the Nautobot app.

The worker's first WebSocket frame is a handshake object::

    {"worker_id": "...", "timestamp": 1700000000, "nonce": "...", "signature": "..."}

where ``signature = HMAC-SHA256("{worker_id}:{timestamp}:{nonce}", session_secret)``
hex-encoded. The gateway never sees ``session_secret``; it performs cheap local
checks (shape, timestamp skew, nonce replay via Redis SETNX+TTL) and then asks
the app's internal verify-session endpoint to validate the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import GatewaySettings, nonce_key

logger = logging.getLogger(__name__)


class HandshakeError(Exception):
    """The handshake frame is malformed or fails a local pre-check."""


class AuthenticationFailed(Exception):
    """The app rejected the session (or could not be reached)."""


class Handshake(BaseModel):
    """The first frame a worker sends on ``/ws/worker``."""

    worker_id: str = Field(min_length=1, max_length=255)
    timestamp: int
    nonce: str = Field(min_length=8, max_length=255)
    signature: str = Field(min_length=1, max_length=512)


@dataclass(frozen=True)
class VerifiedSession:
    """Identity the app returned for an authenticated worker session."""

    worker_id: str
    zone_id: str


def signing_payload(worker_id: str, timestamp: int, nonce: str) -> str:
    """Canonical string the worker signs: ``"{worker_id}:{timestamp}:{nonce}"``."""
    return f"{worker_id}:{timestamp}:{nonce}"


def compute_signature(worker_id: str, timestamp: int, nonce: str, session_secret: str) -> str:
    """Compute the handshake HMAC-SHA256 signature (hex).

    This is the canonical definition of the signature scheme; the worker agent
    and the app's verify-session endpoint must implement the same computation.
    The gateway itself never holds session secrets and only uses this helper
    in tests and documentation.
    """
    return hmac.new(
        session_secret.encode("utf-8"),
        signing_payload(worker_id, timestamp, nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(handshake: Handshake, session_secret: str) -> bool:
    """Constant-time check of a handshake signature against a known secret."""
    expected = compute_signature(handshake.worker_id, handshake.timestamp, handshake.nonce, session_secret)
    return hmac.compare_digest(expected, handshake.signature.lower())


def parse_handshake(payload: Any) -> Handshake:
    """Validate the raw handshake object.

    Raises:
        HandshakeError: when required fields are missing or malformed.
    """
    if not isinstance(payload, dict):
        raise HandshakeError("handshake must be a JSON object")
    try:
        return Handshake.model_validate(payload)
    except ValidationError as exc:
        raise HandshakeError(f"invalid handshake: {exc.error_count()} field error(s)") from exc


def check_timestamp(handshake: Handshake, skew_seconds: int, now: float | None = None) -> None:
    """Reject handshakes whose timestamp is outside the allowed clock skew.

    Raises:
        HandshakeError: when the timestamp is too far from gateway time.
    """
    current = time.time() if now is None else now
    if abs(current - handshake.timestamp) > skew_seconds:
        raise HandshakeError("handshake timestamp outside allowed skew")


async def check_nonce(redis: Any, handshake: Handshake, ttl_seconds: int) -> None:
    """Replay protection: claim the nonce atomically via SET NX with a TTL.

    Raises:
        HandshakeError: when the nonce was already used within the TTL window.
    """
    key = nonce_key(handshake.worker_id, handshake.nonce)
    claimed = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    if not claimed:
        raise HandshakeError("nonce replay detected")


class SessionVerifier:
    """Validates worker handshakes against the app's internal endpoint.

    The gateway authenticates itself to the app with the shared internal token
    (``GATEWAY_INTERNAL_TOKEN``) — the only secret it holds.
    """

    def __init__(self, http_client: httpx.AsyncClient, settings: GatewaySettings) -> None:
        self._http = http_client
        self._settings = settings

    async def verify(self, handshake: Handshake) -> VerifiedSession:
        """POST the handshake to verify-session and return the worker identity.

        Raises:
            AuthenticationFailed: on an invalid session or an unreachable app.
        """
        try:
            response = await self._http.post(
                self._settings.verify_session_url,
                json=handshake.model_dump(),
                headers={"Authorization": f"Token {self._settings.internal_token}"},
                timeout=self._settings.verify_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("verify-session call failed for worker %s: %s", handshake.worker_id, exc)
            raise AuthenticationFailed("verify-session unavailable") from exc

        if response.status_code != 200:
            logger.warning("verify-session returned HTTP %s for worker %s", response.status_code, handshake.worker_id)
            raise AuthenticationFailed(f"verify-session returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise AuthenticationFailed("verify-session returned invalid JSON") from exc

        if not isinstance(body, dict) or not body.get("valid"):
            raise AuthenticationFailed("session rejected by app")

        worker_id = body.get("worker_id")
        zone_id = body.get("zone_id")
        if not isinstance(worker_id, str) or not worker_id or not isinstance(zone_id, str) or not zone_id:
            raise AuthenticationFailed("verify-session response missing worker_id/zone_id")
        return VerifiedSession(worker_id=worker_id, zone_id=zone_id)
