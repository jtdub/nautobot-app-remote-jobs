"""JSON-RPC 2.0 frame validation and error helpers.

The gateway never interprets method semantics; it only classifies frames well
enough to route them (request / notification / response) and to emit protocol
errors for malformed, oversized, or rate-limited traffic.

Error codes: the standard JSON-RPC codes plus the app range from SPEC 8.3
(-32001..-32006). Gateway-originated errors use the implementation-defined
server range (-32000..-32099) above the app codes so they never collide.
"""

from __future__ import annotations

import enum
import json
from typing import Any

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# App error code range (SPEC 8.3) — issued by the app, listed for reference.
ERR_UNAUTHORIZED = -32001
ERR_UNKNOWN_RUN = -32002
ERR_ILLEGAL_TRANSITION = -32003
ERR_LEASE_EXPIRED = -32004
ERR_SINGLETON_HELD = -32005
ERR_DRAINING = -32006

# Gateway-originated errors (implementation-defined server range, gateway slice).
ERR_RATE_LIMITED = -32010
ERR_FRAME_TOO_LARGE = -32011
ERR_UPSTREAM_TIMEOUT = -32012


class FrameType(enum.Enum):
    """Routing classification of a JSON-RPC frame."""

    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"


class FrameError(Exception):
    """A frame failed JSON-RPC validation.

    Attributes:
        code: JSON-RPC error code to report.
        request_id: The frame's ``id`` when one could be extracted, else None.
    """

    def __init__(self, message: str, code: int = INVALID_REQUEST, request_id: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def parse_frame(raw: str) -> dict[str, Any]:
    """Parse raw frame text into a JSON object.

    Raises:
        FrameError: with PARSE_ERROR for invalid JSON, INVALID_REQUEST for
            non-object payloads (batch requests are unsupported per SPEC 8).
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise FrameError(f"invalid JSON: {exc}", code=PARSE_ERROR) from exc
    if not isinstance(obj, dict):
        raise FrameError("frame must be a single JSON object (batches unsupported)", code=INVALID_REQUEST)
    return obj


def classify_frame(frame: dict[str, Any]) -> FrameType:
    """Validate a decoded frame and classify it for routing.

    Rules (JSON-RPC 2.0):
      * ``jsonrpc`` must equal ``"2.0"``.
      * A frame with ``method`` (a non-empty string) is a REQUEST when it
        carries a non-null ``id`` (string or integer), else a NOTIFICATION.
      * A frame without ``method`` must be a RESPONSE: an ``id`` plus exactly
        one of ``result`` / ``error``.

    Raises:
        FrameError: when the frame violates any rule.
    """
    request_id = frame.get("id")
    if frame.get("jsonrpc") != JSONRPC_VERSION:
        raise FrameError("jsonrpc must be '2.0'", request_id=_safe_id(request_id))

    if "id" in frame and not isinstance(request_id, (str, int, type(None))):
        raise FrameError("id must be a string, integer, or null", request_id=None)

    method = frame.get("method")
    if "method" in frame:
        if not isinstance(method, str) or not method:
            raise FrameError("method must be a non-empty string", request_id=_safe_id(request_id))
        params = frame.get("params")
        if "params" in frame and not isinstance(params, (dict, list)):
            raise FrameError("params must be an object or array", request_id=_safe_id(request_id))
        if request_id is None:
            return FrameType.NOTIFICATION
        return FrameType.REQUEST

    # No method: must be a response.
    has_result = "result" in frame
    has_error = "error" in frame
    if has_result == has_error:
        raise FrameError("response must carry exactly one of 'result' or 'error'", request_id=_safe_id(request_id))
    if "id" not in frame:
        raise FrameError("response must carry an 'id'", request_id=None)
    if has_error and not isinstance(frame["error"], dict):
        raise FrameError("error must be an object", request_id=_safe_id(request_id))
    return FrameType.RESPONSE


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response object."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _safe_id(request_id: Any) -> Any:
    """Return the id only when it is a legal JSON-RPC id type."""
    return request_id if isinstance(request_id, (str, int)) else None
