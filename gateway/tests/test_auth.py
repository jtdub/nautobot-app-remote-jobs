"""Handshake signature, timestamp, nonce, and verify-session tests."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from remote_jobs_gateway.auth import (
    AuthenticationFailed,
    Handshake,
    HandshakeError,
    SessionVerifier,
    check_nonce,
    check_timestamp,
    compute_signature,
    parse_handshake,
    verify_signature,
)

from .stubs import InMemoryRedis, make_settings

SECRET = "s3cret-session-secret"


def make_handshake(secret: str = SECRET, **overrides) -> Handshake:
    fields = {"worker_id": "worker-1", "timestamp": 1_700_000_000, "nonce": "abcdef0123456789"}
    fields.update({k: v for k, v in overrides.items() if k != "signature"})
    signature = overrides.get(
        "signature",
        compute_signature(fields["worker_id"], fields["timestamp"], fields["nonce"], secret),
    )
    return Handshake(signature=signature, **fields)


class TestSignature:
    def test_matches_reference_hmac(self):
        """The helper must equal a hand-rolled HMAC-SHA256 over 'wid:ts:nonce'."""
        expected = hmac.new(SECRET.encode(), b"worker-1:1700000000:abcdef0123456789", hashlib.sha256).hexdigest()
        assert compute_signature("worker-1", 1_700_000_000, "abcdef0123456789", SECRET) == expected

    def test_valid_signature_verifies(self):
        assert verify_signature(make_handshake(), SECRET) is True

    def test_wrong_secret_rejected(self):
        assert verify_signature(make_handshake(), "other-secret") is False

    def test_tampered_fields_rejected(self):
        good = make_handshake()
        for field, value in [("worker_id", "worker-2"), ("timestamp", 1_700_000_001), ("nonce", "ffffffffffffffff")]:
            tampered = good.model_copy(update={field: value})
            assert verify_signature(tampered, SECRET) is False, field

    def test_uppercase_hex_signature_accepted(self):
        hs = make_handshake()
        hs = hs.model_copy(update={"signature": hs.signature.upper()})
        assert verify_signature(hs, SECRET) is True


class TestParseHandshake:
    def test_valid_payload(self):
        hs = make_handshake()
        parsed = parse_handshake(json.loads(hs.model_dump_json()))
        assert parsed == hs

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            "string",
            {},
            {"worker_id": "w", "timestamp": 1, "nonce": "abcdefgh"},  # missing signature
            {"worker_id": "", "timestamp": 1, "nonce": "abcdefgh", "signature": "x"},  # empty worker_id
            {"worker_id": "w", "timestamp": "notanint", "nonce": "abcdefgh", "signature": "x"},
            {"worker_id": "w", "timestamp": 1, "nonce": "short", "signature": "x"},  # nonce < 8 chars
        ],
    )
    def test_invalid_payloads_rejected(self, payload):
        with pytest.raises(HandshakeError):
            parse_handshake(payload)


class TestTimestampAndNonce:
    def test_timestamp_within_skew_accepted(self):
        hs = make_handshake()
        check_timestamp(hs, skew_seconds=300, now=hs.timestamp + 299)

    def test_timestamp_outside_skew_rejected(self):
        hs = make_handshake()
        with pytest.raises(HandshakeError):
            check_timestamp(hs, skew_seconds=300, now=hs.timestamp + 301)
        with pytest.raises(HandshakeError):
            check_timestamp(hs, skew_seconds=300, now=hs.timestamp - 301)

    async def test_nonce_replay_rejected(self):
        redis = InMemoryRedis()
        hs = make_handshake()
        await check_nonce(redis, hs, ttl_seconds=60)  # first use OK
        with pytest.raises(HandshakeError):
            await check_nonce(redis, hs, ttl_seconds=60)  # replay blocked

    async def test_different_nonces_independent(self):
        redis = InMemoryRedis()
        await check_nonce(redis, make_handshake(nonce="nonce-aaaaaaa1"), ttl_seconds=60)
        await check_nonce(redis, make_handshake(nonce="nonce-aaaaaaa2"), ttl_seconds=60)


class TestSessionVerifier:
    def make_verifier(self, handler) -> SessionVerifier:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return SessionVerifier(client, make_settings())

    async def test_valid_session(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"valid": True, "worker_id": "worker-1", "zone_id": "zone-9"})

        session = await self.make_verifier(handler).verify(make_handshake())
        assert session.worker_id == "worker-1"
        assert session.zone_id == "zone-9"
        assert seen["auth"] == "Token test-internal-token"
        assert seen["url"] == "http://app.test/api/plugins/remote-jobs/internal/verify-session/"
        assert set(seen["body"]) == {"worker_id", "timestamp", "nonce", "signature"}

    async def test_invalid_session_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"valid": False})

        with pytest.raises(AuthenticationFailed):
            await self.make_verifier(handler).verify(make_handshake())

    async def test_http_error_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "bad token"})

        with pytest.raises(AuthenticationFailed):
            await self.make_verifier(handler).verify(make_handshake())

    async def test_network_failure_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(AuthenticationFailed):
            await self.make_verifier(handler).verify(make_handshake())

    async def test_missing_identity_fields_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"valid": True, "worker_id": "worker-1"})  # no zone_id

        with pytest.raises(AuthenticationFailed):
            await self.make_verifier(handler).verify(make_handshake())
