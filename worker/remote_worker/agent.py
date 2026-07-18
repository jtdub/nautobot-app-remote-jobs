"""Worker agent orchestration (SPEC 8, 12.2).

Owns the gateway connection, capacity accounting and the claim loop,
per-run supervision (status heartbeats, cancel, timeout), drain handling,
journal reconciliation after restarts, and log sink selection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from . import AGENT_VERSION, rpc
from .config import WorkerConfig
from .connection import ConnectionClosedError, GatewayConnection, gateway_ws_url
from .health import HealthServer
from .journal import JournalEntry, RunJournal
from .runtime.base import (
    ContainerHandle,
    ContainerRuntime,
    ContainerSpec,
    ImageReferenceError,
    Mount,
    ensure_digest_reference,
)
from .sinks import LogSink, create_sink
from .state import StateStore, WorkerState

logger = logging.getLogger(__name__)

STATE_SUCCESS = "SUCCESS"
STATE_FAILURE = "FAILURE"
STATE_TERMINATED = "TERMINATED"

_COMPLETE_RETRY_MAX_SECONDS = 30.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def container_name_for_run(run_id: str) -> str:
    """Deterministic container name so restarts can find their containers."""
    return f"remote-job-{run_id}"


class WorkerAgent:
    """Top-level agent: one gateway connection, up to ``capacity`` runs."""

    def __init__(
        self,
        config: WorkerConfig,
        state: WorkerState,
        state_store: StateStore,
        journal: RunJournal,
        runtime: ContainerRuntime,
    ) -> None:
        self.config = config
        self.state = state
        self._state_store = state_store
        self.journal = journal
        self.runtime = runtime
        self.draining = False
        self.closing = False
        self.lease_seconds = 120
        self._runs: dict[str, RunTask] = {}
        self._sink: LogSink | None = None
        self._sink_config_used: dict[str, Any] | None = None
        self._claim_wanted = asyncio.Event()
        self._stopped = asyncio.Event()
        self.connection = GatewayConnection(
            url=gateway_ws_url(config.gateway_url),
            worker_id=state.worker_id,
            secret_provider=lambda: self.state.session_secret,
            on_connected=self._on_connected,
            on_server_call=self._on_server_call,
            backoff_min=config.backoff_min,
            backoff_max=config.backoff_max,
            tls_verify=config.tls_verify,
        )
        self._health = HealthServer(config.health_host, config.health_port, self.health_status)
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Run until :meth:`shutdown` is called."""
        await self._health.start()
        self._tasks = [
            asyncio.create_task(self.connection.run_forever(), name="gateway"),
            asyncio.create_task(self._claim_loop(), name="claim-loop"),
        ]
        await self._stopped.wait()

    async def shutdown(self) -> None:
        """Graceful local shutdown.

        In-flight containers are left running; their journal entries remain
        so a restarted agent re-attaches and the server reconciles via the
        ``in_flight`` list in the next ``worker.hello``.
        """
        logger.info("shutting down agent (in-flight runs: %d)", len(self._runs))
        self.closing = True
        run_tasks = []
        for run in list(self._runs.values()):
            run.detach()
            if run.task is not None:
                run.task.cancel()
                run_tasks.append(run.task)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *run_tasks, return_exceptions=True)
        await self.connection.close()
        if self._sink is not None:
            try:
                await self._sink.close()
            except Exception:
                logger.exception("error closing log sink")
        await self.runtime.close()
        await self._health.close()
        self._stopped.set()

    def health_status(self) -> dict[str, Any]:
        """Snapshot served by ``/healthz``."""
        return {
            "worker_id": self.state.worker_id,
            "agent_version": AGENT_VERSION,
            "connected": self.connection.connected,
            "draining": self.draining,
            "in_flight": len(self._runs),
            "capacity": self.config.capacity,
        }

    # ----------------------------------------------------------- connection

    async def _on_connected(self, connection: GatewayConnection) -> None:
        """(Re)establish session state: worker.hello + reconciliation."""
        in_flight = sorted(set(self._runs) | set(self.journal.run_ids()))
        hello = await connection.call(
            "worker.hello",
            {
                "worker_id": self.state.worker_id,
                "agent_version": AGENT_VERSION,
                "capabilities": self.config.capabilities,
                "capacity": self.config.capacity,
                "in_flight": in_flight,
            },
        )
        hello = hello or {}
        self.lease_seconds = int(hello.get("lease_seconds", self.lease_seconds))
        if hello.get("draining"):
            self.draining = True
        await self._ensure_sink(hello.get("log_sink_config"))
        self._recover_journaled_runs()
        self._claim_wanted.set()
        logger.info(
            "worker.hello acknowledged (draining=%s lease=%ds in_flight=%d)",
            self.draining,
            self.lease_seconds,
            len(in_flight),
        )

    async def _ensure_sink(self, server_config: dict[str, Any] | None) -> None:
        """Build/rebuild the log sink from server config or local override."""
        effective = self.config.log_sink_override or server_config or {"type": "http"}
        if self._sink is not None and effective == self._sink_config_used:
            return
        if self._sink is not None:
            try:
                await self._sink.close()
            except Exception:
                logger.exception("error closing previous log sink")
        self._sink = create_sink(
            effective,
            nautobot_url=self.config.nautobot_url,
            worker_id=self.state.worker_id,
            secret_provider=lambda: self.state.session_secret,
            tls_verify=self.config.tls_verify,
        )
        await self._sink.start()
        self._sink_config_used = effective
        logger.info("log sink configured: type=%s", effective.get("type", "http"))

    @property
    def sink(self) -> LogSink:
        """The active log sink (built lazily before first hello completes)."""
        if self._sink is None:
            raise RuntimeError("log sink not configured yet")
        return self._sink

    async def rotate_session_secret(self) -> None:
        """Rotate the session credential via ``worker.rotate`` (SPEC 7.2)."""
        result = await self.connection.call("worker.rotate", {})
        secret = result.get("session_secret") if isinstance(result, dict) else None
        if not secret:
            raise RuntimeError("worker.rotate returned no session_secret")
        self.state = WorkerState(worker_id=self.state.worker_id, session_secret=str(secret))
        self._state_store.save(self.state)
        logger.info("session secret rotated and persisted")

    # -------------------------------------------------- server->worker RPCs

    async def _on_server_call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "worker.ping":
            return {}
        if method == "worker.drain":
            logger.info("drain requested by server; finishing in-flight runs")
            self.draining = True
            return {}
        if method == "job.available":
            self._claim_wanted.set()
            return {}
        if method == "job.cancel":
            run_id = str(params.get("run_id", ""))
            mode = str(params.get("mode", "graceful"))
            run = self._runs.get(run_id)
            if run is None:
                raise rpc.RpcError(rpc.UNKNOWN_RUN, data={"run_id": run_id})
            asyncio.create_task(run.cancel(mode), name=f"cancel-{run_id}")
            return {"ok": True}
        raise rpc.RpcError(rpc.METHOD_NOT_FOUND, data={"method": method})

    # ------------------------------------------------------------ claiming

    @property
    def free_capacity(self) -> int:
        """Slots available for new offers."""
        return max(0, self.config.capacity - len(self._runs))

    def poke_claim(self) -> None:
        """Ask the claim loop to try claiming again."""
        self._claim_wanted.set()

    async def _claim_loop(self) -> None:
        while True:
            await self._claim_wanted.wait()
            self._claim_wanted.clear()
            await self._maybe_claim()

    async def _maybe_claim(self) -> None:
        if self.draining or self.closing or not self.connection.connected:
            return
        free = self.free_capacity
        if free <= 0:
            return
        try:
            result = await self.connection.call("job.claim", {"max": free})
        except ConnectionClosedError:
            return
        except rpc.RpcError as exc:
            if exc.code == rpc.DRAINING:
                logger.info("server says draining; stopping claims")
                self.draining = True
            else:
                logger.warning("job.claim failed: %s", exc)
            return
        offers = (result or {}).get("offers") or []
        for offer in offers:
            self.start_run(offer)
        if offers:
            # There may be more queued work than `max`; try again.
            self._claim_wanted.set()

    def start_run(self, offer: dict[str, Any], entry: JournalEntry | None = None) -> None:
        """Accept an offer: journal it and launch its supervision task."""
        run_id = str(offer.get("run_id", ""))
        if not run_id:
            logger.error("dropping offer without run_id: %r", offer)
            return
        if run_id in self._runs:
            logger.warning("duplicate offer for run %s ignored", run_id)
            return
        run = RunTask(self, offer, entry)
        self._runs[run_id] = run
        task = asyncio.create_task(run.run(), name=f"run-{run_id}")
        run.task = task
        task.add_done_callback(lambda _t, rid=run_id: self._run_finished(rid))

    def _recover_journaled_runs(self) -> None:
        """Resume/settle journaled runs that have no active task (SPEC 8.1)."""
        for entry in self.journal.load_all():
            if entry.run_id in self._runs:
                continue
            logger.info("recovering journaled run %s", entry.run_id)
            run = RunTask(self, entry.offer, entry)
            self._runs[entry.run_id] = run
            task = asyncio.create_task(run.recover(), name=f"recover-{entry.run_id}")
            run.task = task
            task.add_done_callback(lambda _t, rid=entry.run_id: self._run_finished(rid))

    def _run_finished(self, run_id: str) -> None:
        run = self._runs.pop(run_id, None)
        if run is not None and run.task is not None:
            exc = run.task.exception() if not run.task.cancelled() else None
            if exc is not None:
                logger.error("run task %s crashed: %r", run_id, exc)
        self._claim_wanted.set()


class RunTask:
    """Supervises one run: container lifecycle, heartbeats, cancel, timeout."""

    def __init__(self, agent: WorkerAgent, offer: dict[str, Any], entry: JournalEntry | None = None) -> None:
        self.agent = agent
        self.offer = offer
        self.run_id = str(offer["run_id"])
        self.timeout_seconds = float(offer.get("timeout_seconds", 1800))
        self.grace_seconds = float(offer.get("grace_seconds", 30))
        self.task: asyncio.Task[None] | None = None
        self._entry = entry
        self._handle: ContainerHandle | None = None
        self._exited = asyncio.Event()
        self._terminated = False  # cancel requested (graceful or kill)
        self._timed_out = False
        self._lease_lost = False
        self._cancel_task: asyncio.Task[None] | None = None
        self._complete_sent = False
        self._complete_acked = False
        self._detached = False
        self._image_digest_executed = ""

    # -------------------------------------------------------------- launch

    async def run(self) -> None:
        """Fresh-offer path: journal, pull, create, start, supervise."""
        entry = self._entry or self.agent.journal.add(self.offer)
        self._entry = entry
        image = str(self.offer.get("image", ""))
        try:
            digest = ensure_digest_reference(image)
            self._image_digest_executed = digest
            auth = self.agent.config.registry_auth_for(image)
            await self.agent.runtime.pull(image, auth=auth)
            spec = self._build_spec(image)
            self._handle = await self.agent.runtime.create(spec)
            await self._handle.start()
        except ImageReferenceError as exc:
            logger.error("run %s: %s", self.run_id, exc)
            await self._finalize(STATE_FAILURE, exit_code=None, error=str(exc))
            return
        except Exception as exc:
            logger.exception("run %s: failed to start container", self.run_id)
            await self._finalize(STATE_FAILURE, exit_code=None, error=f"failed to start: {exc}")
            return
        if self._terminated:
            # Cancelled while we were pulling/creating.
            await self._handle.kill()
        deadline = entry.started_at + self.timeout_seconds
        await self._supervise(deadline)

    async def recover(self) -> None:
        """Post-restart path: re-attach to the container if it still exists."""
        entry = self._entry
        if entry is None:
            raise RuntimeError("recover() requires a journal entry")
        name = container_name_for_run(self.run_id)
        image = str(self.offer.get("image", ""))
        try:
            self._image_digest_executed = ensure_digest_reference(image)
        except ImageReferenceError:
            self._image_digest_executed = ""
        try:
            handle = await self.agent.runtime.get_by_name(name)
        except Exception as exc:
            logger.exception("run %s: recovery lookup failed", self.run_id)
            await self._finalize(STATE_FAILURE, exit_code=None, error=f"recovery failed: {exc}")
            return
        if handle is None:
            logger.warning("run %s: container lost across restart", self.run_id)
            await self._finalize(
                STATE_FAILURE,
                exit_code=None,
                error="container lost across agent restart",
            )
            return
        self._handle = handle
        logger.info("run %s: re-attached to container %s", self.run_id, name)
        deadline = entry.started_at + self.timeout_seconds
        await self._supervise(deadline)

    # ---------------------------------------------------------- supervision

    async def _supervise(self, deadline: float) -> None:
        if self._handle is None:
            raise RuntimeError("_supervise() requires a container handle")
        console_task = asyncio.create_task(self._pump_console(), name=f"console-{self.run_id}")
        status_task = asyncio.create_task(self._status_loop(), name=f"status-{self.run_id}")
        exit_code: int | None = None
        error: str | None = None
        try:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            exit_code = await asyncio.wait_for(self._handle.wait(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            if not self._terminated:
                self._timed_out = True
                logger.warning("run %s exceeded timeout of %ds; killing", self.run_id, self.timeout_seconds)
            await self._handle.kill()
            exit_code = await self._safe_wait()
        except asyncio.CancelledError:
            status_task.cancel()
            console_task.cancel()
            raise
        except Exception as exc:
            logger.exception("run %s: error waiting for container", self.run_id)
            error = f"runtime error: {exc}"
            await self._handle.kill()
            exit_code = await self._safe_wait()
        finally:
            self._exited.set()
            status_task.cancel()
            if self._cancel_task is not None:
                # Let a graceful-cancel finish signalling before finalizing.
                try:
                    await asyncio.wait_for(asyncio.shield(self._cancel_task), timeout=5.0)
                except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError, Exception):  # noqa: S110
                    pass
            # Give the console pump a moment to drain remaining output.
            try:
                await asyncio.wait_for(console_task, timeout=10.0)
            except (asyncio.TimeoutError, TimeoutError):
                console_task.cancel()
            except (asyncio.CancelledError, Exception):  # noqa: S110 - best-effort drain
                pass
        state, final_error = self._classify(exit_code, error)
        await self._finalize(state, exit_code=exit_code, error=final_error)

    def _classify(self, exit_code: int | None, error: str | None) -> tuple[str, str | None]:
        """Map termination causes to the wire state per SPEC 8.1/8.2."""
        if self._terminated:
            return STATE_TERMINATED, None
        if self._timed_out:
            return STATE_FAILURE, "timeout"
        if self._lease_lost:
            return STATE_FAILURE, "lease expired"
        if error is not None:
            return STATE_FAILURE, error
        if exit_code == 0:
            return STATE_SUCCESS, None
        return STATE_FAILURE, f"exit code {exit_code}"

    async def _safe_wait(self) -> int | None:
        if self._handle is None:
            return None
        try:
            return await asyncio.wait_for(self._handle.wait(), timeout=30.0)
        except (asyncio.TimeoutError, TimeoutError, Exception):
            return None

    async def _pump_console(self) -> None:
        """Stream container stdout/stderr into the console sink.

        The scoped NAUTOBOT_TOKEN the agent injects into the container env is
        masked from raw console output here: a non-SDK entrypoint (e.g. a shell
        running ``printenv``) writes directly to fd 1/2 and bypasses the SDK's
        in-process redactor, so without this the live token could be persisted
        into JobConsoleEntry (SPEC 10.2 'no tokens'). The worker can only mask
        the token it injected; container-resolved secret values never reach the
        worker and are redacted in-process by the SDK.
        """
        if self._handle is None:
            return
        token = str(self.offer.get("token") or "")
        try:
            async for output_type, text in self._handle.stream_logs():
                if not text:
                    continue
                clean = text.rstrip("\n")
                if token and token in clean:
                    clean = clean.replace(token, "(redacted)")
                await self.agent.sink.emit_console(
                    self.run_id,
                    {
                        "output_type": output_type,
                        "text": clean,
                        "timestamp": _utc_now_iso(),
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run %s: console streaming failed", self.run_id)

    async def _status_loop(self) -> None:
        """job.status every ``status_interval`` (25s): lease renewal + cancel poll."""
        interval = self.agent.config.status_interval
        while not self._exited.is_set():
            try:
                await asyncio.wait_for(self._exited.wait(), timeout=interval)
                return
            except (asyncio.TimeoutError, TimeoutError):
                pass
            if self._detached:
                return
            try:
                result = await self.agent.connection.call("job.status", {"run_id": self.run_id, "state": "RUNNING"})
            except ConnectionClosedError:
                continue  # reconnect loop will restore the session
            except rpc.RpcError as exc:
                if exc.code in (rpc.UNKNOWN_RUN, rpc.LEASE_EXPIRED):
                    logger.error(
                        "run %s: server rejected status (%s); killing container",
                        self.run_id,
                        exc,
                    )
                    self._lease_lost = True
                    if self._handle is not None:
                        await self._handle.kill()
                    return
                logger.warning("run %s: job.status error: %s", self.run_id, exc)
                continue
            if (result or {}).get("cancel_requested") and self._cancel_task is None:
                logger.info("run %s: cancel requested via status poll", self.run_id)
                self._cancel_task = asyncio.create_task(self.cancel("graceful"), name=f"cancel-{self.run_id}")

    # ---------------------------------------------------------------- cancel

    async def cancel(self, mode: str = "graceful") -> None:
        """Handle ``job.cancel`` (SPEC 8.2).

        graceful: SIGTERM, wait ``grace_seconds``, then SIGKILL.
        kill: SIGKILL immediately.
        """
        self._terminated = True
        handle = self._handle
        if handle is None:
            logger.info("run %s: cancel before container start; will not start", self.run_id)
            return
        if mode == "kill":
            await handle.kill()
            return
        logger.info("run %s: graceful cancel (SIGTERM, %.0fs grace)", self.run_id, self.grace_seconds)
        await handle.terminate()
        try:
            await asyncio.wait_for(handle.wait(), timeout=self.grace_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("run %s: grace period expired; SIGKILL", self.run_id)
            await handle.kill()

    def detach(self) -> None:
        """Stop supervising without killing the container (agent shutdown)."""
        self._detached = True

    # -------------------------------------------------------------- finalize

    async def _finalize(self, state: str, exit_code: int | None, error: str | None) -> None:
        """Flush logs, send job.complete, clean up container + journal."""
        if self._detached:
            return
        try:
            await self.agent.sink.flush(self.run_id)
        except Exception:
            logger.exception("run %s: final sink flush failed", self.run_id)
        digest = self._image_digest_executed
        if self._handle is not None:
            reported = await self._handle.image_digest()
            if reported:
                digest = reported
        await self._send_complete(state, exit_code, digest, error)
        await self._cleanup()

    async def _send_complete(self, state: str, exit_code: int | None, digest: str, error: str | None) -> None:
        """Deliver job.complete, retrying across reconnects (idempotent per run)."""
        if self._complete_sent:
            return
        self._complete_sent = True
        params: dict[str, Any] = {
            "run_id": self.run_id,
            "state": state,
            "exit_code": exit_code,
            "image_digest_executed": digest,
        }
        if error:
            params["error"] = error
        delay = 1.0
        while not self.agent.closing:
            try:
                await self.agent.connection.call("job.complete", params)
                self._complete_acked = True
                logger.info("run %s completed: state=%s exit_code=%s", self.run_id, state, exit_code)
                return
            except ConnectionClosedError:
                pass  # wait for reconnect and retry
            except rpc.RpcError as exc:
                # An application-level RPC error means the server received and
                # definitively rejected this completion (unknown run, illegal
                # transition after a reap to ABANDONED, lease expired). Retrying
                # the identical frame only gets the same rejection and would spin
                # forever, pinning the RunTask and its capacity slot and leaving
                # the journal entry behind. Treat it as acknowledged: the run is
                # already terminal server-side (or the reaper will settle it).
                logger.warning("run %s: job.complete rejected (%s); giving up", self.run_id, exc)
                self._complete_acked = True
                return
            await asyncio.sleep(delay)
            delay = min(_COMPLETE_RETRY_MAX_SECONDS, delay * 2)

    async def _cleanup(self) -> None:
        if self._handle is not None:
            try:
                await self._handle.remove()
            except Exception:
                logger.exception("run %s: container removal failed", self.run_id)
        if self._complete_acked:
            self.agent.journal.remove(self.run_id)
            sink = self.agent._sink
            if sink is not None and hasattr(sink, "forget_run"):
                sink.forget_run(self.run_id)

    # ----------------------------------------------------------------- spec

    def _build_spec(self, image: str) -> ContainerSpec:
        """Assemble the hardened container spec from the offer (SPEC 12.2/12.3)."""
        config = self.agent.config
        env: dict[str, str] = {str(key): str(value) for key, value in (self.offer.get("env") or {}).items()}
        env["NAUTOBOT_URL"] = str(self.offer.get("nautobot_url") or config.nautobot_url)
        token = self.offer.get("token")
        if token:
            env["NAUTOBOT_TOKEN"] = str(token)
        env.setdefault("REMOTE_JOBS_RUN_ID", self.run_id)
        env.setdefault("REMOTE_JOBS_ZONE", "")
        env.setdefault("REMOTE_JOBS_DRYRUN", "false")
        # Deliver the validated job inputs (and schema) the SDK reads via
        # Context.from_env; without this ctx.inputs is always empty.
        env["REMOTE_JOBS_INPUTS"] = json.dumps(self.offer.get("inputs") or {})
        schema = self.offer.get("input_schema")
        if schema:
            env["REMOTE_JOBS_INPUT_SCHEMA"] = json.dumps(schema)
        # Allowlisted pass-through env; never overrides offer-provided values.
        for name in config.pass_env:
            if name in os.environ:
                env.setdefault(name, os.environ[name])
        mounts = [Mount(source=mount.source, target=mount.target, read_only=True) for mount in config.secret_mounts]
        nano_cpus = int(config.cpu_limit * 1_000_000_000) if config.cpu_limit else None
        return ContainerSpec(
            name=container_name_for_run(self.run_id),
            image=image,
            env=env,
            mounts=mounts,
            memory_bytes=config.memory_limit_bytes,
            nano_cpus=nano_cpus,
        )
