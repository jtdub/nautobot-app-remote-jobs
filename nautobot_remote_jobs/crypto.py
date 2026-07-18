"""Worker session credential derivation and handshake verification (SPEC 7.2).

The session secret is never stored: it is derived as
HMAC-SHA256(Django SECRET_KEY, "remote-jobs:{worker_id}:{generation}") so the
app can recompute it whenever it must verify a worker signature, while the DB
holds only the SHA-256 fingerprint for audit and pinning. Rotation increments
the worker's secret_generation counter, invalidating the previous secret.
"""

import hashlib
import hmac
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# Accept handshake timestamps within this many seconds of server time.
HANDSHAKE_MAX_SKEW_SECONDS = 300


def derive_session_secret(worker):
    """Deterministically derive the worker's current session secret."""
    message = f"remote-jobs:{worker.pk}:{worker.secret_generation}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def compute_signature(session_secret, worker_id, timestamp, nonce):
    """HMAC-SHA256 hex over "{worker_id}:{timestamp}:{nonce}" (SPEC 7.2)."""
    message = f"{worker_id}:{timestamp}:{nonce}".encode()
    return hmac.new(session_secret.encode(), message, hashlib.sha256).hexdigest()


def verify_handshake(worker, worker_id, timestamp, nonce, signature):
    """Validate a gateway-relayed handshake for the given worker.

    Nonce replay protection is enforced by the gateway (Redis SETNX); the app
    additionally bounds timestamp skew.
    """
    try:
        skew = abs(time.time() - float(timestamp))
    except (TypeError, ValueError):
        return False
    if skew > HANDSHAKE_MAX_SKEW_SECONDS:
        return False
    expected = compute_signature(derive_session_secret(worker), worker_id, timestamp, nonce)
    return hmac.compare_digest(expected, str(signature))


def verify_session_secret(worker, presented_secret):
    """Constant-time check of a directly presented session secret (worker REST auth)."""
    expected = derive_session_secret(worker)
    return hmac.compare_digest(expected, str(presented_secret))
