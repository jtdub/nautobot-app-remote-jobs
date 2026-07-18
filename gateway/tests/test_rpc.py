"""JSON-RPC 2.0 frame validation tests."""

from __future__ import annotations

import json

import pytest
from remote_jobs_gateway import rpc


class TestParseFrame:
    def test_valid_object(self):
        assert rpc.parse_frame('{"jsonrpc": "2.0", "method": "job.claim"}')["method"] == "job.claim"

    def test_invalid_json_is_parse_error(self):
        with pytest.raises(rpc.FrameError) as excinfo:
            rpc.parse_frame("{not json")
        assert excinfo.value.code == rpc.PARSE_ERROR

    @pytest.mark.parametrize("raw", ["[]", '"str"', "42", "null"])
    def test_non_object_rejected(self, raw):
        with pytest.raises(rpc.FrameError) as excinfo:
            rpc.parse_frame(raw)
        assert excinfo.value.code == rpc.INVALID_REQUEST


class TestClassifyFrame:
    def test_request(self):
        frame = {"jsonrpc": "2.0", "id": "abc", "method": "job.claim", "params": {"max": 2}}
        assert rpc.classify_frame(frame) is rpc.FrameType.REQUEST

    def test_request_with_int_id(self):
        assert rpc.classify_frame({"jsonrpc": "2.0", "id": 7, "method": "worker.hello"}) is rpc.FrameType.REQUEST

    def test_notification(self):
        frame = {"jsonrpc": "2.0", "method": "job.available", "params": {"zone": "z"}}
        assert rpc.classify_frame(frame) is rpc.FrameType.NOTIFICATION

    def test_result_response(self):
        assert rpc.classify_frame({"jsonrpc": "2.0", "id": "abc", "result": {}}) is rpc.FrameType.RESPONSE

    def test_error_response(self):
        frame = {"jsonrpc": "2.0", "id": "abc", "error": {"code": -32006, "message": "draining"}}
        assert rpc.classify_frame(frame) is rpc.FrameType.RESPONSE

    @pytest.mark.parametrize(
        "frame",
        [
            {"method": "job.claim", "id": "x"},  # missing jsonrpc
            {"jsonrpc": "1.0", "method": "job.claim", "id": "x"},  # wrong version
            {"jsonrpc": "2.0", "method": "", "id": "x"},  # empty method
            {"jsonrpc": "2.0", "method": 42, "id": "x"},  # non-string method
            {"jsonrpc": "2.0", "method": "m", "params": "notobj", "id": "x"},  # bad params
            {"jsonrpc": "2.0", "id": {"bad": "type"}, "method": "m"},  # bad id type
            {"jsonrpc": "2.0", "id": "x"},  # response without result/error
            {"jsonrpc": "2.0", "id": "x", "result": 1, "error": {"code": 1, "message": "m"}},  # both
            {"jsonrpc": "2.0", "result": 1},  # response without id
            {"jsonrpc": "2.0", "id": "x", "error": "notobj"},  # non-object error
        ],
    )
    def test_invalid_frames_rejected(self, frame):
        with pytest.raises(rpc.FrameError):
            rpc.classify_frame(frame)


class TestErrorResponse:
    def test_shape(self):
        resp = rpc.error_response("id-1", rpc.ERR_RATE_LIMITED, "slow down", data={"limit": 30})
        assert resp == {
            "jsonrpc": "2.0",
            "id": "id-1",
            "error": {"code": -32010, "message": "slow down", "data": {"limit": 30}},
        }
        json.dumps(resp)  # serializable

    def test_null_id_allowed(self):
        assert rpc.error_response(None, rpc.PARSE_ERROR, "bad json")["id"] is None

    def test_gateway_codes_do_not_collide_with_app_range(self):
        app_codes = {
            rpc.ERR_UNAUTHORIZED,
            rpc.ERR_UNKNOWN_RUN,
            rpc.ERR_ILLEGAL_TRANSITION,
            rpc.ERR_LEASE_EXPIRED,
            rpc.ERR_SINGLETON_HELD,
            rpc.ERR_DRAINING,
        }
        gateway_codes = {rpc.ERR_RATE_LIMITED, rpc.ERR_FRAME_TOO_LARGE, rpc.ERR_UPSTREAM_TIMEOUT}
        assert not app_codes & gateway_codes
        assert all(-32099 <= code <= -32000 for code in app_codes | gateway_codes)
