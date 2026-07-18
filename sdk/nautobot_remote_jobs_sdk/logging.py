"""Structured job logging: buffered HTTP sink + stdout echo + redaction.

``ctx.logger`` produces structured entries at the Nautobot ``JobLogEntry``
levels (``debug``/``info``/``success``/``warning``/``error``/``failure``).
Entries are:

1. passed through the redactor (:mod:`nautobot_remote_jobs_sdk.redaction`),
2. echoed to stdout so the worker agent's console capture still works,
3. buffered and flushed in batches to the app's HTTP log endpoint
   ``/api/plugins/remote-jobs/runs/{run_id}/logs/``.

Flush policy (SPEC 10.1): 2 seconds elapsed, 100 entries, or 64 KiB,
whichever comes first. Each batch carries a monotonically increasing
``client_sequence`` so the server can dedupe at-least-once delivery.
"""

from __future__ import annotations

import json
import logging as std_logging
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TextIO

import requests

from . import redaction
from .http import join_url

_module_logger = std_logging.getLogger(__name__)

#: Valid levels, matching Nautobot ``LogLevelChoices`` for ``JobLogEntry``.
LOG_LEVELS = ("debug", "info", "success", "warning", "error", "failure")

FLUSH_INTERVAL_SECONDS = 2.0
FLUSH_MAX_ENTRIES = 100
FLUSH_MAX_BYTES = 64 * 1024


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LogClient:
    """Buffered batch shipper for the remote-jobs HTTP log sink."""

    def __init__(
        self,
        session: requests.Session,
        nautobot_url: str,
        run_id: str,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        max_entries: int = FLUSH_MAX_ENTRIES,
        max_bytes: int = FLUSH_MAX_BYTES,
    ) -> None:
        self._session = session
        self.endpoint = join_url(nautobot_url, "api/plugins/remote-jobs/runs", run_id, "logs")
        self.flush_interval = flush_interval
        self.max_entries = max_entries
        self.max_bytes = max_bytes

        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        self._buffered_bytes = 0
        self._sequence = 0
        self._last_flush = time.monotonic()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- buffering ---------------------------------------------------------

    def add(self, entry: Dict[str, Any]) -> None:
        """Buffer one structured entry, flushing if a threshold is hit."""
        encoded = len(json.dumps(entry, default=str).encode("utf-8"))
        with self._lock:
            self._entries.append(entry)
            self._buffered_bytes += encoded
            if (
                len(self._entries) >= self.max_entries
                or self._buffered_bytes >= self.max_bytes
                or (time.monotonic() - self._last_flush) >= self.flush_interval
            ):
                self._flush_locked()

    def flush(self) -> None:
        """Flush any buffered entries immediately."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        self._last_flush = time.monotonic()
        if not self._entries:
            return
        batch = self._entries
        self._entries = []
        self._buffered_bytes = 0
        self._sequence += 1
        payload = {"client_sequence": self._sequence, "entries": batch}
        try:
            response = self._session.post(self.endpoint, json=payload, timeout=30)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - logging must never crash the job
            _module_logger.warning("Failed to ship log batch %s: %s", self._sequence, exc)

    # -- background flusher ------------------------------------------------

    def start(self) -> None:
        """Start the background thread enforcing the 2s time-based flush."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._flush_loop, name="remote-jobs-log-flusher", daemon=True
        )
        self._thread.start()

    def _flush_loop(self) -> None:
        while not self._stop_event.wait(self.flush_interval):
            self.flush()

    def stop(self) -> None:
        """Stop the background thread and perform a final flush."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.flush_interval * 2)
            self._thread = None
        self.flush()


class JobLogger:
    """Structured logger exposed as ``ctx.logger``.

    Every method accepts a ``grouping=`` keyword mapped to
    ``JobLogEntry.grouping``. Messages are redacted before they are buffered
    or echoed.
    """

    def __init__(
        self,
        client: Optional[LogClient] = None,
        echo_stream: Optional[TextIO] = None,
    ) -> None:
        self._client = client
        self._echo_stream = echo_stream

    def log(self, level: str, message: str, grouping: Optional[str] = None) -> None:
        """Emit one structured entry at *level*."""
        if level not in LOG_LEVELS:
            raise ValueError(f"Unknown log level {level!r}; expected one of {LOG_LEVELS}")
        message = redaction.redact(str(message))
        entry: Dict[str, Any] = {
            "level": level,
            "message": message,
            "grouping": grouping or "run",
            "timestamp": _utcnow_iso(),
        }
        self._echo(entry)
        if self._client is not None:
            self._client.add(entry)

    def _echo(self, entry: Dict[str, Any]) -> None:
        stream = self._echo_stream if self._echo_stream is not None else sys.stdout
        try:
            stream.write(
                f"{entry['timestamp']} [{entry['level'].upper()}] "
                f"{entry['grouping']}: {entry['message']}\n"
            )
            stream.flush()
        except Exception:  # noqa: BLE001 - echo failures must not break the job
            pass

    def debug(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``debug`` level."""
        self.log("debug", message, grouping=grouping)

    def info(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``info`` level."""
        self.log("info", message, grouping=grouping)

    def success(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``success`` level."""
        self.log("success", message, grouping=grouping)

    def warning(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``warning`` level."""
        self.log("warning", message, grouping=grouping)

    def error(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``error`` level."""
        self.log("error", message, grouping=grouping)

    def failure(self, message: str, grouping: Optional[str] = None) -> None:
        """Log at ``failure`` level."""
        self.log("failure", message, grouping=grouping)

    def flush(self) -> None:
        """Flush buffered entries to the HTTP sink."""
        if self._client is not None:
            self._client.flush()
