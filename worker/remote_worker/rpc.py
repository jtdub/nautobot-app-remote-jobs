"""JSON-RPC 2.0 framing and error codes (SPEC 8, 8.3).

Transport rules: one JSON-RPC 2.0 object per WebSocket text frame, batch
requests unsupported, request ids are UUIDv4 strings, notifications carry
no ``id``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application error range (SPEC 8.3).
UNAUTHORIZED = -32001
UNKNOWN_RUN = -32002
ILLEGAL_TRANSITION = -32003
LEASE_EXPIRED = -32004
SINGLETON_HELD = -32005
DRAINING = -32006

ERROR_MESSAGES: dict[int, str] = {
    PARSE_ERROR: "parse error",
    INVALID_REQUEST: "invalid request",
    METHOD_NOT_FOUND: "method not found",
    INVALID_PARAMS: "invalid params",
    INTERNAL_ERROR: "internal error",
    UNAUTHORIZED: "unauthorized",
    UNKNOWN_RUN: "unknown_run",
    ILLEGAL_TRANSITION: "illegal_transition",
    LEASE_EXPIRED: "lease_expired",
    SINGLETON_HELD: "singleton_held",
    DRAINING: "draining",
}


class RpcError(Exception):
    """A JSON-RPC error, either received from the peer or raised locally."""

    def __init__(self, code: int, message: str | None = None, data: Any = None) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "error")
        self.data = data
        super().__init__(f"[{code}] {self.message}")

    def to_error_object(self) -> dict[str, Any]:
        """Render as a JSON-RPC ``error`` member."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


@dataclass
class RpcRequest:
    """An incoming request or notification (``id is None`` for notifications)."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str | None = None

    @property
    def is_notification(self) -> bool:
        """True when the peer expects no response."""
        return self.id is None


@dataclass
class RpcResponse:
    """An incoming response to one of our requests."""

    id: str | None
    result: Any = None
    error: dict[str, Any] | None = None

    def raise_for_error(self) -> Any:
        """Return ``result`` or raise :class:`RpcError` if this is an error."""
        if self.error is not None:
            raise RpcError(
                code=int(self.error.get("code", INTERNAL_ERROR)),
                message=self.error.get("message"),
                data=self.error.get("data"),
            )
        return self.result


def new_id() -> str:
    """A fresh UUIDv4 request id."""
    return str(uuid.uuid4())


def request_frame(method: str, params: dict[str, Any] | None = None, id: str | None = None) -> dict[str, Any]:
    """Build a request frame (generates a UUIDv4 id when omitted)."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": id if id is not None else new_id(),
        "method": method,
        "params": params or {},
    }


def notification_frame(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a notification frame (no id, no response expected)."""
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}}


def result_frame(id: str, result: Any) -> dict[str, Any]:
    """Build a success response frame."""
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def error_frame(id: str | None, code: int, message: str | None = None, data: Any = None) -> dict[str, Any]:
    """Build an error response frame."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "error": RpcError(code, message, data).to_error_object(),
    }


def encode(frame: dict[str, Any]) -> str:
    """Serialize a frame for the wire."""
    return json.dumps(frame, separators=(",", ":"), default=str)


def decode(text: str | bytes) -> RpcRequest | RpcResponse:
    """Parse a single text frame into a request/notification or a response.

    Raises:
        RpcError: with ``PARSE_ERROR`` for invalid JSON, ``INVALID_REQUEST``
            for structurally invalid frames (including batches, which the
            wire protocol forbids).
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RpcError(PARSE_ERROR, data=str(exc)) from exc
    if not isinstance(obj, dict):
        raise RpcError(
            INVALID_REQUEST, "batch requests are not supported" if isinstance(obj, list) else "frame must be an object"
        )
    if obj.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(INVALID_REQUEST, "missing or invalid jsonrpc version")
    if "method" in obj:
        method = obj["method"]
        params = obj.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            raise RpcError(INVALID_REQUEST, "invalid method or params")
        req_id = obj.get("id")
        return RpcRequest(method=method, params=params, id=str(req_id) if req_id is not None else None)
    if "result" in obj or "error" in obj:
        resp_id = obj.get("id")
        error = obj.get("error")
        if error is not None and not isinstance(error, dict):
            raise RpcError(INVALID_REQUEST, "invalid error member")
        return RpcResponse(id=str(resp_id) if resp_id is not None else None, result=obj.get("result"), error=error)
    raise RpcError(INVALID_REQUEST, "frame is neither request nor response")
