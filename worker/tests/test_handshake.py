"""Tests for the HMAC session handshake (SPEC 7.2)."""

import hashlib
import hmac

from remote_worker.connection import build_handshake, compute_signature, gateway_ws_url


def test_signature_matches_reference_hmac():
    worker_id = "8a4c9c0e-7d43-4b52-9d3e-000000000001"
    secret = "super-secret-session-credential"
    timestamp = 1752796800
    nonce = "abcdef0123456789"
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{worker_id}:{timestamp}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert compute_signature(worker_id, timestamp, nonce, secret) == expected


def test_signature_known_vector():
    # Frozen vector so any accidental change to the message format fails loudly.
    signature = compute_signature("w1", 1700000000, "n1", "k1")
    expected = hmac.new(b"k1", b"w1:1700000000:n1", hashlib.sha256).hexdigest()
    assert signature == expected
    assert len(signature) == 64
    assert all(c in "0123456789abcdef" for c in signature)


def test_build_handshake_fields_and_validity():
    frame = build_handshake("worker-1", "secret", timestamp=123, nonce="deadbeef")
    assert set(frame) == {"worker_id", "timestamp", "nonce", "signature"}
    assert frame["worker_id"] == "worker-1"
    assert frame["timestamp"] == 123
    assert frame["nonce"] == "deadbeef"
    assert frame["signature"] == compute_signature("worker-1", 123, "deadbeef", "secret")


def test_build_handshake_generates_fresh_nonce():
    one = build_handshake("worker-1", "secret")
    two = build_handshake("worker-1", "secret")
    assert one["nonce"] != two["nonce"]
    assert one["signature"] != two["signature"]


def test_signature_depends_on_every_component():
    base = compute_signature("w", 1, "n", "s")
    assert compute_signature("w2", 1, "n", "s") != base
    assert compute_signature("w", 2, "n", "s") != base
    assert compute_signature("w", 1, "n2", "s") != base
    assert compute_signature("w", 1, "n", "s2") != base


def test_gateway_ws_url_scheme_mapping():
    assert gateway_ws_url("https://gw.example.com") == "wss://gw.example.com/ws/worker"
    assert gateway_ws_url("http://gw.example.com/") == "ws://gw.example.com/ws/worker"
    assert gateway_ws_url("wss://gw.example.com/ws/worker") == "wss://gw.example.com/ws/worker"
