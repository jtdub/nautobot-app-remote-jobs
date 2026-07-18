"""GraphQL helper for bulk reads (``ctx.graphql``).

SPEC guidance: prefer GraphQL for reads touching more than ~100 objects;
per-object REST GETs are the new N+1.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .http import join_url


class GraphQLError(RuntimeError):
    """Raised when the GraphQL endpoint returns errors."""

    def __init__(self, errors: List[Any]) -> None:
        self.errors = errors
        super().__init__(f"GraphQL query returned errors: {errors!r}")


class GraphQLClient:
    """Callable client for ``POST {NAUTOBOT_URL}/api/graphql/``."""

    def __init__(self, session: requests.Session, nautobot_url: str) -> None:
        self._session = session
        self.endpoint = join_url(nautobot_url, "api/graphql")

    def __call__(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute *query* and return the ``data`` payload.

        Raises:
            GraphQLError: when the response contains an ``errors`` list.
            requests.HTTPError: on non-2xx responses.
        """
        body: Dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        response = self._session.post(self.endpoint, json=body, timeout=120)
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise GraphQLError(payload["errors"])
        return payload.get("data", {})
