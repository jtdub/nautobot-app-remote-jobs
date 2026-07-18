"""Log sink interface and shared batching behaviour (SPEC 10).

Two record kinds flow through a sink:

- ``logs``: structured entries ``{level, message, grouping, timestamp}``
  destined for ``JobLogEntry``.
- ``console``: stdout/stderr lines ``{output_type, text, timestamp}``
  destined for ``JobConsoleEntry``.

Batching (SPEC 10.1): flush a per-(run, kind) buffer on whichever comes
first of 2 seconds elapsed, 100 entries, or 64 KiB of serialized entries.
Every flushed batch carries a monotonically increasing ``client_sequence``
per run so the server can dedupe at-least-once delivery.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

KIND_LOGS = "logs"
KIND_CONSOLE = "console"

FLUSH_INTERVAL_SECONDS = 2.0
FLUSH_MAX_ENTRIES = 100
FLUSH_MAX_BYTES = 64 * 1024
_POLL_SECONDS = 0.25


class LogSink(abc.ABC):
    """Destination for structured log entries and console output."""

    @abc.abstractmethod
    async def emit_log(self, run_id: str, entry: dict[str, Any]) -> None:
        """Queue one structured log entry for *run_id*."""

    @abc.abstractmethod
    async def emit_console(self, run_id: str, entry: dict[str, Any]) -> None:
        """Queue one console line for *run_id*."""

    @abc.abstractmethod
    async def flush(self, run_id: str | None = None) -> None:
        """Flush buffered entries (for one run, or all runs)."""

    async def start(self) -> None:
        """Start any background tasks. Default: nothing."""

    async def close(self) -> None:
        """Flush everything and release resources. Default: flush."""
        await self.flush()


@dataclass
class _Buffer:
    """Pending entries for one (run_id, kind)."""

    first_at: float
    entries: list[dict[str, Any]] = field(default_factory=list)
    byte_size: int = 0


class BatchingLogSink(LogSink):
    """Base class implementing the 2s / 100 entries / 64 KiB flush policy.

    Subclasses implement :meth:`_send` to deliver one batch. ``_send`` is
    invoked while the sink lock is held, which serializes batches and keeps
    ``client_sequence`` ordering per run.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._buffers: dict[tuple[str, str], _Buffer] = {}
        self._sequences: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._flusher: asyncio.Task[None] | None = None

    # ----------------------------------------------------------- interface

    async def emit_log(self, run_id: str, entry: dict[str, Any]) -> None:
        await self._add(run_id, KIND_LOGS, entry)

    async def emit_console(self, run_id: str, entry: dict[str, Any]) -> None:
        await self._add(run_id, KIND_CONSOLE, entry)

    async def flush(self, run_id: str | None = None) -> None:
        async with self._lock:
            keys = [key for key in self._buffers if run_id is None or key[0] == run_id]
            for key in keys:
                await self._flush_locked(key)

    async def start(self) -> None:
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop(), name="sink-flusher")

    async def close(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        await self.flush()
        await self._close()

    async def _close(self) -> None:
        """Subclass hook to release transport resources."""

    # ------------------------------------------------------------ batching

    async def _add(self, run_id: str, kind: str, entry: dict[str, Any]) -> None:
        async with self._lock:
            key = (run_id, kind)
            buffer = self._buffers.get(key)
            if buffer is None:
                buffer = self._buffers[key] = _Buffer(first_at=self._clock())
            buffer.entries.append(entry)
            buffer.byte_size += len(json.dumps(entry, default=str).encode("utf-8"))
            if (
                len(buffer.entries) >= FLUSH_MAX_ENTRIES
                or buffer.byte_size >= FLUSH_MAX_BYTES
            ):
                await self._flush_locked(key)

    async def maybe_flush_expired(self) -> None:
        """Flush buffers older than :data:`FLUSH_INTERVAL_SECONDS`."""
        now = self._clock()
        async with self._lock:
            expired = [
                key
                for key, buffer in self._buffers.items()
                if now - buffer.first_at >= FLUSH_INTERVAL_SECONDS
            ]
            for key in expired:
                await self._flush_locked(key)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_POLL_SECONDS)
            try:
                await self.maybe_flush_expired()
            except Exception:  # pragma: no cover - defensive
                logger.exception("unexpected error flushing log sink")

    async def _flush_locked(self, key: tuple[str, str]) -> None:
        """Pop and send one buffer. Caller must hold ``self._lock``."""
        buffer = self._buffers.pop(key, None)
        if buffer is None or not buffer.entries:
            return
        run_id, kind = key
        sequence = self._sequences.get(run_id, 0) + 1
        self._sequences[run_id] = sequence
        try:
            await self._send(run_id, kind, sequence, buffer.entries)
        except Exception:
            logger.exception(
                "failed to deliver %s batch seq=%d for run %s (%d entries dropped)",
                kind, sequence, run_id, len(buffer.entries),
            )

    def next_sequence_hint(self, run_id: str) -> int:
        """The last sequence assigned for *run_id* (0 when none)."""
        return self._sequences.get(run_id, 0)

    def forget_run(self, run_id: str) -> None:
        """Drop sequence bookkeeping for a finished run."""
        self._sequences.pop(run_id, None)

    @abc.abstractmethod
    async def _send(
        self, run_id: str, kind: str, sequence: int, entries: list[dict[str, Any]]
    ) -> None:
        """Deliver one batch. Must be at-least-once (retry internally)."""
