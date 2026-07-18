"""Tests for JSON-RPC 2.0 framing and error codes (SPEC 8, 8.3)."""

import json
import uuid

import pytest
from remote_worker import rpc


def test_error_code_constants():
    assert rpc.UNAUTHORIZED == -32001
    assert rpc.UNKNOWN_RUN == -32002
    assert rpc.ILLEGAL_TRANSITION == -32003
    assert rpc.LEASE_EXPIRED == -32004
    assert rpc.SINGLETON_HELD == -32005
    assert rpc.DRAINING == -32006
    assert rpc.PARSE_ERROR == -32700
    assert rpc.INVALID_REQUEST == -32600
    assert rpc.METHOD_NOT_FOUND == -32601


def test_request_frame_has_uuid4_id():
    frame = rpc.request_frame("worker.hello", {"capacity": 4})
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "worker.hello"
    assert frame["params"] == {"capacity": 4}
    parsed = uuid.UUID(frame["id"])
    assert parsed.version == 4


def test_notification_frame_has_no_id():
    frame = rpc.notification_frame("job.available", {"zone": "dfw"})
    assert "id" not in frame
    assert frame["method"] == "job.available"


def test_encode_decode_request_roundtrip():
    frame = rpc.request_frame("job.claim", {"max": 2})
    decoded = rpc.decode(rpc.encode(frame))
    assert isinstance(decoded, rpc.RpcRequest)
    assert decoded.method == "job.claim"
    assert decoded.params == {"max": 2}
    assert decoded.id == frame["id"]
    assert not decoded.is_notification


def test_decode_notification():
    decoded = rpc.decode(json.dumps({"jsonrpc": "2.0", "method": "worker.ping", "params": {}}))
    assert isinstance(decoded, rpc.RpcRequest)
    assert decoded.is_notification


def test_decode_result_response():
    text = json.dumps({"jsonrpc": "2.0", "id": "abc", "result": {"ok": True}})
    decoded = rpc.decode(text)
    assert isinstance(decoded, rpc.RpcResponse)
    assert decoded.raise_for_error() == {"ok": True}


def test_decode_error_response_raises_on_unwrap():
    text = json.dumps(
        {"jsonrpc": "2.0", "id": "abc", "error": {"code": -32005, "message": "singleton_held", "data": {"run_id": "x"}}}
    )
    decoded = rpc.decode(text)
    assert isinstance(decoded, rpc.RpcResponse)
    with pytest.raises(rpc.RpcError) as excinfo:
        decoded.raise_for_error()
    assert excinfo.value.code == rpc.SINGLETON_HELD
    assert excinfo.value.data == {"run_id": "x"}


def test_decode_invalid_json_is_parse_error():
    with pytest.raises(rpc.RpcError) as excinfo:
        rpc.decode("{not json")
    assert excinfo.value.code == rpc.PARSE_ERROR


def test_decode_batch_rejected():
    with pytest.raises(rpc.RpcError) as excinfo:
        rpc.decode(json.dumps([{"jsonrpc": "2.0", "method": "a"}]))
    assert excinfo.value.code == rpc.INVALID_REQUEST


def test_decode_missing_version_rejected():
    with pytest.raises(rpc.RpcError) as excinfo:
        rpc.decode(json.dumps({"method": "worker.ping"}))
    assert excinfo.value.code == rpc.INVALID_REQUEST


def test_error_frame_shape():
    frame = rpc.error_frame("id-1", rpc.UNKNOWN_RUN, data={"run_id": "r"})
    assert frame["error"]["code"] == -32002
    assert frame["error"]["message"] == "unknown_run"
    assert frame["error"]["data"] == {"run_id": "r"}


def test_result_frame_shape():
    frame = rpc.result_frame("id-2", {"offers": []})
    assert frame == {"jsonrpc": "2.0", "id": "id-2", "result": {"offers": []}}
