"""Server-side JSON-RPC method handlers (SPEC 8.1).

Called by the Redis bridge consumer (management command remote_jobs_rpc_consumer).
Each handler receives the authenticated Worker and the request params dict and
returns the JSON-RPC result payload, or raises RPCError.
"""

import logging

from nautobot_remote_jobs.choices import RunStateChoices
from nautobot_remote_jobs.constants import RPC_DRAINING, RPC_UNAUTHORIZED
from nautobot_remote_jobs.dispatch import claims
from nautobot_remote_jobs.models import Worker

logger = logging.getLogger(__name__)

# JSON-RPC standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RPCError(Exception):
    """JSON-RPC error carrying a code (standard or app range, SPEC 8.3)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def to_error(self):
        """JSON-RPC error object."""
        return {"code": self.code, "message": str(self)}


def handle_worker_hello(worker, params):
    """worker.hello: reconcile in-flight runs and return server config (SPEC 8.1)."""
    updates = []
    for field in ("agent_version", "capabilities", "capacity"):
        if field in params and getattr(worker, field) != params[field]:
            setattr(worker, field, params[field])
            updates.append(field)
    if updates:
        worker.save(update_fields=updates)
    worker.touch()
    claims.reconcile_in_flight(worker, params.get("in_flight") or [])
    return claims.hello_result(worker)


def handle_job_claim(worker, params):
    """job.claim: return up to max offers; offers count toward capacity immediately."""
    if worker.draining:
        raise RPCError(RPC_DRAINING, "Worker is draining")
    if not worker.enabled:
        raise RPCError(RPC_UNAUTHORIZED, "Worker is disabled")
    max_offers = int(params.get("max", 1))
    try:
        offers = claims.claim_runs(worker, max_offers=max_offers)
    except claims.ClaimError as exc:
        raise RPCError(exc.code, str(exc)) from exc
    return {"offers": offers}


def handle_job_status(worker, params):
    """job.status: lease renewal + cancel poll (SPEC 8.1)."""
    _require(params, "run_id")
    try:
        return claims.report_status(
            worker,
            params["run_id"],
            state=params.get("state", RunStateChoices.RUNNING),
            progress=params.get("progress"),
        )
    except claims.ClaimError as exc:
        raise RPCError(exc.code, str(exc)) from exc


def handle_job_complete(worker, params):
    """job.complete: idempotent terminal report (SPEC 8.1)."""
    _require(params, "run_id", "state")
    try:
        return claims.complete_run(
            worker,
            params["run_id"],
            state=params["state"],
            exit_code=params.get("exit_code"),
            image_digest_executed=params.get("image_digest_executed", ""),
            error=params.get("error"),
        )
    except claims.ClaimError as exc:
        raise RPCError(exc.code, str(exc)) from exc


def handle_worker_rotate(worker, params):  # pylint: disable=unused-argument
    """worker.rotate: issue a new session secret; old one invalidated immediately (SPEC 7.2).

    Secrets are derived, not stored (see nautobot_remote_jobs.crypto); rotation
    bumps the generation counter. The new secret is returned over the
    established (TLS) session only; the DB stores only its fingerprint.
    """
    from nautobot_remote_jobs.crypto import derive_session_secret

    worker.secret_generation += 1
    new_secret = derive_session_secret(worker)
    worker.identity_fingerprint = Worker.fingerprint(new_secret)
    worker.save(update_fields=["secret_generation", "identity_fingerprint"])
    return {"session_secret": new_secret}


METHODS = {
    "worker.hello": handle_worker_hello,
    "job.claim": handle_job_claim,
    "job.status": handle_job_status,
    "job.complete": handle_job_complete,
    "worker.rotate": handle_worker_rotate,
}


def dispatch_rpc(worker_id, frame):
    """Route one worker->server JSON-RPC frame to its handler.

    Returns a JSON-RPC response object, or None for notifications.
    """
    request_id = frame.get("id")

    def error(code, message):
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    if frame.get("jsonrpc") != "2.0" or not isinstance(frame.get("method"), str):
        return error(INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
    method = frame["method"]
    handler = METHODS.get(method)
    if handler is None:
        return error(METHOD_NOT_FOUND, f"Unknown method {method}")
    try:
        worker = Worker.objects.select_related("zone").get(pk=worker_id)
    except (Worker.DoesNotExist, ValueError):
        return error(RPC_UNAUTHORIZED, "Unknown worker")
    params = frame.get("params") or {}
    if not isinstance(params, dict):
        return error(INVALID_PARAMS, "params must be an object")
    try:
        result = handler(worker, params)
    except RPCError as exc:
        logger.info("RPC %s from worker %s failed: %s", method, worker.name, exc)
        return None if request_id is None else {"jsonrpc": "2.0", "id": request_id, "error": exc.to_error()}
    except Exception:  # noqa: BLE001 - consumer must never crash on one bad frame
        logger.exception("Unhandled error in RPC %s from worker %s", method, worker.name)
        return error(INTERNAL_ERROR, "Internal error")
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _require(params, *keys):
    missing = [key for key in keys if key not in params]
    if missing:
        raise RPCError(INVALID_PARAMS, f"Missing params: {', '.join(missing)}")
