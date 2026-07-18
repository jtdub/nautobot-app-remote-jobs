"""Shared HTTP session construction with retry/backoff.

All SDK HTTP traffic (logging sink, secrets directory lookups, GraphQL,
artifacts, the publish CLI) goes through sessions built here so retry
behavior is uniform.
"""

from __future__ import annotations

from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._version import __version__

USER_AGENT = f"nautobot-remote-jobs-sdk/{__version__}"

#: HTTP statuses considered transient and retried.
RETRY_STATUSES = (429, 500, 502, 503, 504)


def build_session(
    token: Optional[str] = None,
    retries: int = 3,
    backoff_factor: float = 1.0,
) -> requests.Session:
    """Return a :class:`requests.Session` with retry/backoff mounted.

    Args:
        token: Nautobot API token; when given, sent as ``Authorization: Token ...``.
        retries: Total retry attempts for connection errors and
            :data:`RETRY_STATUSES` responses.
        backoff_factor: urllib3 exponential backoff factor between retries.
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept"] = "application/json"
    if token:
        session.headers["Authorization"] = f"Token {token}"
    if retries:
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=retries,
                backoff_factor=backoff_factor,
                allowed_methods=None,  # retry idempotent-unsafe methods too; batches dedupe server-side
                status_forcelist=list(RETRY_STATUSES),
                raise_on_status=False,
            )
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def join_url(base: str, *parts: str) -> str:
    """Join URL segments with exactly one ``/`` between them, trailing slash kept."""
    url = base.rstrip("/")
    for part in parts:
        url = f"{url}/{part.strip('/')}"
    return url + "/"
