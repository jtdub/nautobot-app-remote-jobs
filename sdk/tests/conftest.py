"""Shared offline test fixtures: a tiny fake requests session, no network."""

from __future__ import annotations

import json as jsonlib
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pytest
import requests

from nautobot_remote_jobs_sdk import redaction


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.text = text or (jsonlib.dumps(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("No JSON body")
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]


Handler = Union[FakeResponse, Callable[..., FakeResponse]]


class FakeSession:
    """Route-based fake for requests.Session.

    Routes are ``(method, url_substring, response_or_callable)`` tuples,
    matched first-wins. Every call is recorded in ``self.calls`` as
    ``(method, url, kwargs)``.
    """

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.routes: List[Tuple[str, str, Handler]] = []
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def add(self, method: str, url_substring: str, handler: Handler) -> None:
        self.routes.append((method.upper(), url_substring, handler))

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for route_method, substring, handler in self.routes:
            if route_method == method and substring in url:
                if callable(handler):
                    return handler(method, url, **kwargs)
                return handler
        return FakeResponse(status_code=404, json_data={"detail": f"no route for {method} {url}"})

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("PATCH", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("HEAD", url, **kwargs)

    # helpers -----------------------------------------------------------

    def calls_for(self, method: str, url_substring: str) -> List[Tuple[str, str, Dict[str, Any]]]:
        return [
            call
            for call in self.calls
            if call[0] == method.upper() and url_substring in call[1]
        ]


@pytest.fixture()
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture(autouse=True)
def _clean_redactor():
    """Keep the module-level redaction registry isolated between tests."""
    redaction.clear()
    yield
    redaction.clear()
