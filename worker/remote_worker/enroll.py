"""Enrollment flow (SPEC 7.1).

On first boot, with no persisted session state, the agent exchanges its
single-purpose enrollment token for a worker identity::

    POST {REMOTE_JOBS_URL}/api/plugins/remote-jobs/enroll/
    Authorization: Token <enroll token>
    {"name": ..., "capabilities": [...], "capacity": N, "agent_version": ...}

The response ``{worker_id, session_secret}`` is persisted on the state
volume and the enrollment token is discarded (never written to disk).
"""

from __future__ import annotations

import logging

import httpx

from . import AGENT_VERSION
from .config import WorkerConfig
from .state import WorkerState

logger = logging.getLogger(__name__)

ENROLL_PATH = "/api/plugins/remote-jobs/enroll/"


class EnrollmentError(RuntimeError):
    """Raised when the enrollment exchange fails."""


async def enroll(config: WorkerConfig) -> WorkerState:
    """Exchange the enrollment token for ``{worker_id, session_secret}``."""
    if not config.enroll_token:
        raise EnrollmentError(
            "no persisted worker state and REMOTE_JOBS_ENROLL_TOKEN is not set; "
            "create an enrollment token in Nautobot and provide it on first boot"
        )
    url = config.nautobot_url.rstrip("/") + ENROLL_PATH
    body = {
        "name": config.name,
        "capabilities": config.capabilities,
        "capacity": config.capacity,
        "agent_version": AGENT_VERSION,
    }
    logger.info("enrolling worker %r at %s", config.name, url)
    async with httpx.AsyncClient(verify=config.tls_verify, timeout=30.0) as client:
        try:
            response = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Token {config.enroll_token}"},
            )
        except httpx.HTTPError as exc:
            raise EnrollmentError(f"enrollment request failed: {exc}") from exc
    if response.status_code not in (200, 201):
        raise EnrollmentError(
            f"enrollment rejected: HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        data = response.json()
        state = WorkerState(
            worker_id=str(data["worker_id"]),
            session_secret=str(data["session_secret"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise EnrollmentError(f"malformed enrollment response: {exc}") from exc
    logger.info("enrolled as worker_id=%s", state.worker_id)
    return state
